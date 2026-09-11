"""Font backends for the deterministic PDF writer.

The writer previously supported exactly one text path: base-14 Helvetica
with WinAnsi (cp1252) encoding, where every character cp1252 cannot
represent was silently replaced by ``?``. This module adds the missing
capability as a *font backend* abstraction, per the design contract:

* **Renderer MUST preserve arbitrary Unicode text supplied by the state
  layer.** Characters the font cannot map are surfaced as warnings and
  drawn as the font's ``.notdef`` glyph -- never silently replaced by
  ``?``. Where a character has a single-character NFKD base present in
  the font (e.g. the superscript minus of ``cm^-1``), the base glyph is
  drawn under its own subset glyph id with the *original* character in
  ToUnicode, so the text stays extractable and legible (a warning is
  still emitted).

* ``Base14Backend`` keeps the legacy WinAnsi path byte-identical
  (``strict=False`` reproduces the historical ``?`` fallback for the
  low-level writer; ``strict=True`` raises instead, for product paths
  that must never degrade silently).

* ``TrueTypeBackend`` embeds TrueType (glyf) fonts as ``Type0`` /
  ``CIDFontType2`` with ``Identity-H`` encoding, a ``ToUnicode`` CMap
  (text stays extractable/searchable) and a deterministic glyph subset.
  Subsetting is a pure function of (font file, first-use order of
  characters), so repeated renders of the same call sequence remain
  byte-identical.

* ``language`` selects labels only. Encoding is decided by the *content*:
  callers scan the text to render and pick a backend; the backend never
  consults the language pack.

Font files are resolved by :class:`FontConfig`: the bundled
``assets/fonts/`` directory of the skill installation, overridable with
``SCIENTIFIC_REPRODUCTION_FONT_DIR`` so deployments can pin their own
fonts (see ``assets/fonts/README.md``).

Everything here is stdlib-only and deterministic -- no wall clock, no
randomness, no third-party dependencies.
"""

from __future__ import annotations

import os
import struct
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from scientific_reproduction.rendering import style as _style

# ---------------------------------------------------------------------------
# Font file resolution
# ---------------------------------------------------------------------------

ENV_FONT_DIR: Final[str] = "SCIENTIFIC_REPRODUCTION_FONT_DIR"

#: File names inside a font directory, keyed by program role.
REGULAR_FILE: Final[str] = "msyh.ttc"
BOLD_FILE: Final[str] = "msyhbd.ttc"


class FontConfigError(ValueError):
    """A required font file cannot be located."""


def _system_font_candidates() -> list[tuple[Path, Path]]:
    """Candidate (regular, bold) CJK font pairs found on this host.

    These reference fonts that already live on the machine (Windows
    system fonts, Linux distro CJK packages, macOS system faces) -- they
    are *referenced*, never copied or redistributed, so bundling-free
    rendering stays inside the OS license for on-machine use. The first
    pair where both files exist wins.
    """
    import platform as _platform

    system = _platform.system()
    candidates: list[tuple[Path, Path]] = []
    if system == "Windows":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        fonts_dir = Path(windir) / "Fonts"
        candidates.append((fonts_dir / "msyh.ttc", fonts_dir / "msyhbd.ttc"))
        # Older zh-CN builds may only ship the non-bold face pair variants.
        candidates.append((fonts_dir / "msyh.ttc", fonts_dir / "simhei.ttf"))
    elif system == "Linux":
        candidates.append(
            (
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
            )
        )
        candidates.append(
            (
                Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
                Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf"),
            )
        )
        candidates.append(
            (
                Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            )
        )
    elif system == "Darwin":
        candidates.append(
            (
                Path("/System/Library/Fonts/PingFang.ttc"),
                Path("/System/Library/Fonts/PingFang.ttc"),
            )
        )
        candidates.append(
            (
                Path("/System/Library/Fonts/STHeiti Light.ttc"),
                Path("/System/Library/Fonts/STHeiti Medium.ttc"),
            )
        )
    return [(p1, p2) for p1, p2 in candidates if p1.is_file() and p2.is_file()]


@dataclass(frozen=True)
class FontConfig:
    """Font file locations for the Unicode backend.

    ``regular`` / ``bold`` are the TrueType (glyf) font files; both may
    be TTC containers (the first font of the collection is used).
    """

    regular: Path
    bold: Path

    @classmethod
    def default(cls) -> "FontConfig":
        """Resolve font files for the Unicode backend -- in priority order:

        1. ``SCIENTIFIC_REPRODUCTION_FONT_DIR`` environment override
           (explicit deployment pin, always wins);
        2. system CJK fonts on this host (Windows YaHei / Linux Noto &
           WenQuanYi / macOS PingFang -- referenced, not redistributed);
        3. the skill installation's bundled ``assets/fonts/`` directory.

        A missing font file raises :class:`FontConfigError` with an
        actionable hint -- fonts are never guessed.
        """
        override = os.environ.get(ENV_FONT_DIR)
        if override:
            base = Path(override)
            regular = base / REGULAR_FILE
            bold = base / BOLD_FILE
        else:
            system_pair = _system_font_candidates()
            if system_pair:
                return cls(regular=system_pair[0][0], bold=system_pair[0][1])
            # rendering/fonts.py -> 3 levels up = skill installation root.
            base = Path(__file__).resolve().parents[3] / "assets" / "fonts"
            regular = base / REGULAR_FILE
            bold = base / BOLD_FILE
        missing = [str(p) for p in (regular, bold) if not p.is_file()]
        if missing:
            import platform as _platform

            if _platform.system() == "Windows":
                hint = (
                    "system CJK fonts not found either; verify that the Windows "
                    "Fonts directory is intact"
                )
            else:
                hint = (
                    "no system CJK font found; install one (e.g. `apt install "
                    "fonts-noto-cjk` on Debian/Ubuntu) or set a font directory"
                )
            raise FontConfigError(
                f"Unicode backend font files missing: {', '.join(missing)};"
                f" set {ENV_FONT_DIR} to a directory containing"
                f" {REGULAR_FILE} and {BOLD_FILE} (TrueType/TTC), or {hint}"
            )
        return cls(regular=regular, bold=bold)


# ---------------------------------------------------------------------------
# Backend abstraction
# ---------------------------------------------------------------------------

class FontBackend:
    """The font/encoding contract the PDF writer draws text through.

    Logical faces are the four style faces (``style.FONT_FACES``); each
    maps to a font program the backend owns. Text encoding for content
    streams and literal strings (outline titles), measurement, and the
    PDF font resources are all backend responsibilities, so the writer
    never decides character sets itself.
    """

    is_base14: bool = False

    def encode_text(self, text: str, font: str) -> bytes:
        """PDF content-stream string bytes for ``text`` in ``font``."""
        raise NotImplementedError

    def encode_literal(self, text: str) -> bytes:
        """PDF literal string bytes (outline /Title) for ``text``."""
        raise NotImplementedError

    def measure(self, text: str, font: str, size: float) -> float:
        """Width of ``text`` in points at ``size``."""
        raise NotImplementedError

    def pdf_font_objects(self) -> list[bytes]:
        """Bodies of the font dictionary objects, one per logical face
        (F1..F4), in canonical order."""
        raise NotImplementedError

    def pdf_program_objects(self) -> list[bytes]:
        """Bodies of the embedded font-program stream objects."""
        raise NotImplementedError

    def pdf_tounicode_objects(self) -> list[bytes]:
        """Bodies of the ToUnicode CMap stream objects, one per logical
        face, in canonical order."""
        raise NotImplementedError

    def warnings(self) -> list[str]:
        """Warnings accumulated while drawing (e.g. unmapped characters)."""
        raise NotImplementedError


def _escape_literal_bytes(raw: bytes) -> bytes:
    """Escape raw bytes for a PDF literal string (syntax only)."""
    out = bytearray()
    for byte in raw:
        if byte < 0x20 or byte >= 0x7F or byte in (0x28, 0x29, 0x5C):
            out += b"\\" + f"{byte:03o}".encode("ascii")
        else:
            out.append(byte)
    return bytes(out)


def _escape_winansi(text: str) -> bytes:
    """Legacy WinAnsi content-string encoding (kept for Base14Backend)."""
    encoded = text.encode("cp1252", errors="replace")
    return _escape_literal_bytes(encoded)


class Base14Backend(FontBackend):
    """Legacy base-14 Helvetica / WinAnsi path.

    ``strict=False`` reproduces the historical behavior exactly
    (unmappable characters become ``?``); ``strict=True`` raises
    :class:`ValueError` instead, so product paths can guarantee text is
    never silently degraded.
    """

    is_base14 = True

    def __init__(self, strict: bool = False) -> None:
        self.strict = strict
        self._warnings: list[str] = []

    def _check(self, text: str) -> None:
        if not self.strict:
            return
        try:
            text.encode("cp1252")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "Base14Backend(strict=True) cannot encode"
                f" {exc.start}:{exc.end} of {text!r}: character"
                f" {text[exc.start]!r} is outside WinAnsi; use a Unicode"
                " backend for this content"
            ) from None

    def encode_text(self, text: str, font: str) -> bytes:
        """Complete content string: a literal ``(...)`` string."""
        self._check(text)
        return b"(" + _escape_winansi(text) + b")"

    def encode_literal(self, text: str) -> bytes:
        """Complete literal string including the parentheses."""
        self._check(text)
        return b"(" + _escape_winansi(text) + b")"

    def measure(self, text: str, font: str, size: float) -> float:
        return _style.text_width_afm(text, font, size)

    def pdf_font_objects(self) -> list[bytes]:
        return [
            (
                f"<< /Type /Font /Subtype /Type1 /BaseFont /{face} "
                f"/Encoding /WinAnsiEncoding >>"
            ).encode("ascii")
            for face in _style.FONT_FACES
        ]

    def pdf_program_objects(self) -> list[bytes]:
        return []

    def pdf_tounicode_objects(self) -> list[bytes]:
        return []

    def warnings(self) -> list[str]:
        return list(self._warnings)


# ---------------------------------------------------------------------------
# TrueType parsing (stdlib struct only)
# ---------------------------------------------------------------------------

@dataclass
class _Table:
    tag: str
    offset: int
    length: int
    checksum: int


def _load_sfnt(data: bytes) -> tuple[dict[str, _Table], bytes]:
    """Parse a sfnt container (TTC or plain TTF) into its table map.

    Returns ``(tables, font_bytes)`` where ``font_bytes`` starts at the
    sfnt version header of the first font (TTC header skipped). Table
    offsets are stored as absolute positions in ``data``: inside a TTC
    the directory offsets are relative to the *file* start, in a plain
    TTF relative to the sfnt header (offset 0).
    """
    tag = data[:4]
    if tag == b"ttcf":
        # TTC header: 'ttcf', major/minor version, numFonts, offset
        # table; the first offset names the first font's sfnt header.
        base = struct.unpack(">I", data[12:16])[0]
        offsets_relative_to = 0  # TTC directory offsets are file-absolute
    elif tag in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
        base = 0
        offsets_relative_to = 0
    else:
        raise ValueError(f"not a TrueType/TTC font file: tag {tag!r}")
    if data[base : base + 4] not in (b"\x00\x01\x00\x00", b"OTTO"):
        raise ValueError("font at TTC offset is not a sfnt font")
    num_tables = struct.unpack(">H", data[base + 4 : base + 6])[0]
    tables: dict[str, _Table] = {}
    for index in range(num_tables):
        entry = base + 12 + 16 * index
        tag_, checksum, offset, length = struct.unpack(
            ">4sIII", data[entry : entry + 16]
        )
        tables[tag_.decode("latin1")] = _Table(
            tag_.decode("latin1"), offsets_relative_to + offset, length, checksum
        )
    return tables, data[base:]


def _table_bytes(tables: dict[str, _Table], data: bytes, tag: str) -> bytes:
    table = tables[tag]
    return data[table.offset : table.offset + table.length]


class _TrueTypeFont:
    """Read-only TrueType font: cmap, glyph metrics, glyph programs.

    Only the tables the Unicode backend needs are parsed: ``head``,
    ``hhea``, ``maxp``, ``hmtx``, ``loca``, ``glyf``, ``cmap``, ``post``
    and the optional instruction tables (``cvt ``/``fpgm``/``prep``).
    """

    def __init__(self, path: Path) -> None:
        raw = path.read_bytes()
        self.tables, _ = _load_sfnt(raw)
        # Table offsets are absolute positions in the file (TTC) or from
        # the sfnt header (plain TTF at offset 0) -- keep the full bytes.
        self.font_data = raw
        self._require("head", "hhea", "maxp", "hmtx", "loca", "glyf", "cmap")

        head = _table_bytes(self.tables, self.font_data, "head")
        if head[12:16] != b"\x5f\x0f\x3c\xf5":
            raise ValueError(f"{path}: bad head magicNumber")
        self.units_per_em = struct.unpack(">H", head[18:20])[0]
        self.index_to_loc_format = struct.unpack(">h", head[50:52])[0]
        self.bbox = tuple(
            struct.unpack(">hhhh", head[36:44])
        )  # xMin, yMin, xMax, yMax

        hhea = _table_bytes(self.tables, self.font_data, "hhea")
        self.ascender = struct.unpack(">h", hhea[4:6])[0]
        self.descender = struct.unpack(">h", hhea[6:8])[0]
        self.number_of_h_metrics = struct.unpack(">H", hhea[34:36])[0]

        maxp = _table_bytes(self.tables, self.font_data, "maxp")
        self.num_glyphs = struct.unpack(">H", maxp[4:6])[0]

        hmtx = _table_bytes(self.tables, self.font_data, "hmtx")
        self.advances: list[int] = []
        self.left_side_bearings: list[int] = []
        for gid in range(self.number_of_h_metrics):
            advance, lsb = struct.unpack(">Hh", hmtx[4 * gid : 4 * gid + 4])
            self.advances.append(advance)
            self.left_side_bearings.append(lsb)
        if self.num_glyphs > self.number_of_h_metrics:
            last = self.advances[-1]
            lsb_base = 4 * self.number_of_h_metrics
            for gid in range(self.number_of_h_metrics, self.num_glyphs):
                self.advances.append(last)
                entry = lsb_base + 2 * (gid - self.number_of_h_metrics)
                self.left_side_bearings.append(
                    struct.unpack(">h", hmtx[entry : entry + 2])[0]
                )

        loca = _table_bytes(self.tables, self.font_data, "loca")
        if self.index_to_loc_format == 0:
            self.glyph_offsets = [
                struct.unpack(">H", loca[2 * i : 2 * i + 2])[0] * 2
                for i in range(self.num_glyphs + 1)
            ]
        else:
            self.glyph_offsets = [
                struct.unpack(">I", loca[4 * i : 4 * i + 4])[0]
                for i in range(self.num_glyphs + 1)
            ]
        self.glyf = _table_bytes(self.tables, self.font_data, "glyf")

        self.char_to_gid = self._build_cmap()
        try:
            post = _table_bytes(self.tables, self.font_data, "post")
            self.italic_angle = struct.unpack(">i", post[4:8])[0]
            self.underline_position = struct.unpack(">h", post[8:10])[0]
            self.underline_thickness = struct.unpack(">h", post[10:12])[0]
            self.is_fixed_pitch = struct.unpack(">I", post[12:16])[0]
        except KeyError:
            self.italic_angle = 0
            self.underline_position = -75
            self.underline_thickness = 50
            self.is_fixed_pitch = 0

    def _require(self, *tags: str) -> None:
        for tag in tags:
            if tag not in self.tables:
                raise ValueError(f"font lacks required table {tag!r}")

    def _build_cmap(self) -> dict[int, int]:
        """char code -> glyph id; prefers (3,10) format 12, then
        (3,1) format 4."""
        data = _table_bytes(self.tables, self.font_data, "cmap")
        num_tables = struct.unpack(">H", data[2:4])[0]
        records = []
        for index in range(num_tables):
            entry = 4 + 8 * index
            platform, encoding, offset = struct.unpack(
                ">HHI", data[entry : entry + 8]
            )
            records.append((platform, encoding, offset))
        mapping: dict[int, int] = {}
        for platform, encoding, offset in records:
            if (platform, encoding) == (3, 10):
                mapping.update(self._cmap_format12(data, offset))
            elif (platform, encoding) == (3, 1) and not mapping:
                mapping.update(self._cmap_format4(data, offset))
        return mapping

    @staticmethod
    def _cmap_format12(data: bytes, offset: int) -> dict[int, int]:
        fmt = struct.unpack(">H", data[offset : offset + 2])[0]
        if fmt != 12:
            return {}
        num_groups = struct.unpack(">I", data[offset + 12 : offset + 16])[0]
        mapping: dict[int, int] = {}
        for index in range(num_groups):
            entry = offset + 16 + 12 * index
            start, end, start_gid = struct.unpack(
                ">III", data[entry : entry + 12]
            )
            for code in range(start, end + 1):
                mapping[code] = start_gid + (code - start)
        return mapping

    @staticmethod
    def _cmap_format4(data: bytes, offset: int) -> dict[int, int]:
        fmt = struct.unpack(">H", data[offset : offset + 2])[0]
        if fmt != 4:
            return {}
        seg_count = struct.unpack(">H", data[offset + 6 : offset + 8])[0] // 2
        ends = struct.unpack(
            f">{seg_count}H", data[offset + 14 : offset + 14 + 2 * seg_count]
        )
        starts = struct.unpack(
            f">{seg_count}H",
            data[offset + 16 + 2 * seg_count : offset + 16 + 4 * seg_count],
        )
        deltas = struct.unpack(
            f">{seg_count}h",
            data[offset + 16 + 4 * seg_count : offset + 16 + 6 * seg_count],
        )
        range_offsets = struct.unpack(
            f">{seg_count}H",
            data[offset + 16 + 6 * seg_count : offset + 16 + 8 * seg_count],
        )
        mapping: dict[int, int] = {}
        for index in range(seg_count):
            end = ends[index]
            if end == 0xFFFF:
                break
            start = starts[index]
            delta = deltas[index]
            range_offset = range_offsets[index]
            for code in range(start, end + 1):
                if range_offset == 0:
                    gid = (code + delta) & 0xFFFF
                else:
                    glyph_addr = (
                        offset
                        + 16
                        + 8 * seg_count
                        + (index * 2)
                        + range_offset
                        + 2 * (code - start)
                    )
                    gid_raw = struct.unpack(
                        ">H", data[glyph_addr : glyph_addr + 2]
                    )[0]
                    gid = (gid_raw + delta) & 0xFFFF if gid_raw else 0
                mapping[code] = gid
        return mapping

    def glyph_data(self, gid: int) -> bytes:
        return self.glyf[
            self.glyph_offsets[gid] : self.glyph_offsets[gid + 1]
        ]

    def glyph_header(self, gid: int) -> tuple[int, int, int, int, int]:
        data = self.glyph_data(gid)
        if len(data) < 10:
            return 0, 0, 0, 0, 0  # empty glyph: no contours, empty bbox
        contours, x_min, y_min, x_max, y_max = struct.unpack(
            ">hhhhh", data[:10]
        )
        return contours, x_min, y_min, x_max, y_max

    def composite_components(self, gid: int) -> list[int]:
        """Component glyph ids of a composite glyph ([] for simple)."""
        data = self.glyph_data(gid)
        if len(data) < 10:
            return []  # empty glyph (loca[i] == loca[i+1]) is legal
        contours = struct.unpack(">h", data[:2])[0]
        if contours >= 0:
            return []
        components: list[int] = []
        pos = 10
        more = True
        while more and pos + 4 <= len(data):
            flags, component = struct.unpack(">HH", data[pos : pos + 4])
            pos += 4
            components.append(component)
            if flags & 0x0001:  # ARG_1_AND_2_ARE_WORDS
                pos += 4
            else:
                pos += 2
            if flags & 0x0008:  # WE_HAVE_A_SCALE
                pos += 2
            elif flags & 0x0040:  # WE_HAVE_AN_X_AND_Y_SCALE
                pos += 4
            elif flags & 0x0080:  # WE_HAVE_A_TWO_BY_TWO
                pos += 8
            more = bool(flags & 0x0020)  # MORE_COMPONENTS
        return components


# ---------------------------------------------------------------------------
# TrueType backend
# ---------------------------------------------------------------------------

def _fallback_char(char: str) -> str | None:
    """The single-character NFKD base of ``char``, if it differs.

    E.g. U+207B (superscript minus) -> ``-``. Characters without a
    decomposition (emoji, most CJK) return ``None`` and stay ``.notdef``.
    """
    if len(char) != 1:
        return None
    norm = unicodedata.normalize("NFKD", char)
    if len(norm) == 1 and norm != char:
        return norm
    return None


@dataclass
class _Program:
    """One font program (regular or bold) plus its draw-time state.

    New glyph ids are assigned in first-use order at draw time (0 is
    ``.notdef``), so the content streams and the deterministic subset
    agree on numbering without a post-pass remap.
    """

    role: str
    font: _TrueTypeFont
    base_font_name: str
    used: dict[int, int] = field(default_factory=dict)  # old gid -> char
    assigned: dict[int, int] = field(default_factory=dict)  # old -> new gid
    synthetic: dict[int, tuple[int, int]] = field(
        default_factory=dict
    )  # new gid -> (base old gid, char code)
    synth_by_char: dict[int, int] = field(
        default_factory=dict
    )  # char code -> synthetic new gid
    next_gid: int = 1
    missing: list[str] = field(default_factory=list)  # chars without gid
    synthesized: list[tuple[str, str]] = field(
        default_factory=list
    )  # (char, base) drawn via NFKD fallback


def _utf16be_hex(value: int) -> str:
    return f"{value:04X}"


def _checksum(data: bytes) -> int:
    """Big-endian 32-bit sum over ``data`` padded to a u32 boundary."""
    if len(data) % 4:
        data = data + b"\x00" * (4 - len(data) % 4)
    total = 0
    for index in range(0, len(data), 4):
        total = (total + struct.unpack(">I", data[index : index + 4])[0]) & 0xFFFFFFFF
    return total


class TrueTypeBackend(FontBackend):
    """Unicode backend embedding two TrueType fonts (regular + bold).

    Face mapping: ``Helvetica`` -> regular, ``Helvetica-Bold`` -> bold,
    ``Helvetica-Oblique`` -> regular, ``Helvetica-BoldOblique`` -> bold.
    Content strings are hex strings of 2-byte glyph ids (``Identity-H``);
    literal strings are UTF-16BE with BOM. Glyph ids are assigned by the
    deterministic subset (sorted by original gid, ``.notdef`` first), so
    identical draw sequences produce identical bytes.
    """

    is_base14 = False

    def __init__(
        self,
        config: FontConfig,
        *,
        regular_name: str = "UnicodeCJK-Regular",
        bold_name: str = "UnicodeCJK-Bold",
    ) -> None:
        self._regular = _Program(
            "regular", _TrueTypeFont(config.regular), regular_name
        )
        self._bold = _Program("bold", _TrueTypeFont(config.bold), bold_name)

    # -- program selection ------------------------------------------------

    def _program(self, font: str) -> _Program:
        if font in (
            _style.FONT_HELVETICA_BOLD,
            _style.FONT_HELVETICA_BOLD_OBLIQUE,
        ):
            return self._bold
        return self._regular

    def _programs_in_face_order(self) -> list[_Program]:
        return [
            self._program(face) for face in _style.FONT_FACES
        ]

    # -- encoding ----------------------------------------------------------

    def _gid(self, program: _Program, char: str) -> int:
        """The subset glyph id for ``char`` (first-use assignment).

        A character the font cannot map falls back to the glyph of its
        single-character NFKD base when the font has that base (e.g.
        U+207B draws ``-``): the base glyph gets its own subset gid so
        ToUnicode can keep the original character, and a warning is
        emitted. Without a base the character draws ``.notdef``.
        """
        code = ord(char)
        gid = program.font.char_to_gid.get(code)
        if gid is None or gid >= program.font.num_glyphs:
            base = _fallback_char(char)
            if base is not None:
                base_gid = program.font.char_to_gid.get(ord(base))
                if base_gid is not None and base_gid < program.font.num_glyphs:
                    new = program.synth_by_char.get(code)
                    if new is None:
                        new = program.next_gid
                        program.synth_by_char[code] = new
                        program.synthetic[new] = (base_gid, code)
                        program.synthesized.append((char, base))
                        program.next_gid += 1
                    return new
            program.missing.append(char)
            return 0  # .notdef
        new = program.assigned.get(gid)
        if new is None:
            new = program.next_gid
            program.assigned[gid] = new
            program.used[gid] = code
            program.next_gid += 1
        return new

    def encode_text(self, text: str, font: str) -> bytes:
        program = self._program(font)
        out = bytearray(b"<")
        for char in text:
            out += f"{self._gid(program, char):04X}".encode("ascii")
        out += b">"
        return bytes(out)

    def encode_literal(self, text: str) -> bytes:
        raw = b"\xfe\xff" + text.encode("utf-16-be")
        return b"(" + _escape_literal_bytes(raw) + b")"

    # -- measurement -------------------------------------------------------

    def measure(self, text: str, font: str, size: float) -> float:
        program = self._program(font)
        upem = program.font.units_per_em
        total = 0
        for char in text:
            gid = program.font.char_to_gid.get(ord(char))
            if gid is None or gid >= program.font.num_glyphs:
                base = _fallback_char(char)
                gid = (
                    program.font.char_to_gid.get(ord(base))
                    if base is not None
                    else None
                )
                if gid is None or gid >= program.font.num_glyphs:
                    gid = 0
            total += program.font.advances[gid]
        return total * size / upem

    def warnings(self) -> list[str]:
        out: list[str] = []
        for program in (self._regular, self._bold):
            for char, base in dict.fromkeys(program.synthesized):
                out.append(
                    f"character {char!r} (U+{ord(char):04X}) is not in the"
                    f" {program.role} font; drawn from its NFKD base {base!r}"
                )
            for char in dict.fromkeys(program.missing):
                out.append(
                    f"character {char!r} (U+{ord(char):04X}) is not in the"
                    f" {program.role} font; rendered as .notdef"
                )
        return out

    # -- deterministic subsetting ------------------------------------------

    def _subset_glyphs(
        self, program: _Program
    ) -> tuple[list[int], dict[int, int]]:
        """Glyph closure in deterministic order.

        Returns ``(order, char_by_new)``: ``order[new_gid]`` is the
        source glyph id the subset glyph draws (``.notdef`` first, then
        the first-use order of drawn glyphs -- including synthetic NFKD
        fallbacks -- then composite components in breadth-first
        discovery order), and ``char_by_new`` maps each drawn subset
        glyph id to the character it renders (for the subset cmap and
        the ToUnicode CMap).
        """
        source_by_new: dict[int, int] = {0: 0}
        char_by_new: dict[int, int] = {}
        for old, new in program.assigned.items():
            source_by_new[new] = old
            char_by_new[new] = program.used[old]
        for new, (base, code) in program.synthetic.items():
            source_by_new[new] = base
            char_by_new[new] = code
        order = [source_by_new[gid] for gid in range(len(source_by_new))]
        seen = set(order)
        queue = list(order[1:])
        while queue:
            gid = queue.pop(0)
            for component in program.font.composite_components(gid):
                if component not in seen:
                    seen.add(component)
                    order.append(component)
                    queue.append(component)
        return order, char_by_new

    def _subset_font(self, program: _Program) -> bytes:
        font = program.font
        order, char_by_new = self._subset_glyphs(program)
        num_glyphs = len(order)

        # glyf/loca (long format) -------------------------------------------------
        glyf_parts: list[bytes] = []
        loca_offsets: list[int] = [0]  # loca[0] = offset before glyph 0
        offset = 0
        for old_gid in order:
            data = font.glyph_data(old_gid)
            glyf_parts.append(data)
            offset += len(data)
            loca_offsets.append(offset)
        glyf_new = b"".join(glyf_parts)
        loca_new = struct.pack(f">{num_glyphs + 1}I", *loca_offsets)

        # hmtx: every new glyph gets its own metric pair ----------------------
        hmtx_new = bytearray()
        for old_gid in order:
            hmtx_new += struct.pack(
                ">Hh",
                font.advances[old_gid],
                font.left_side_bearings[old_gid],
            )

        # head: long loca, zeroed checksum adjustment, real bbox ---------------
        head = bytearray(_table_bytes(font.tables, font.font_data, "head"))
        head[50:52] = struct.pack(">h", 1)
        head[8:12] = b"\x00\x00\x00\x00"
        headers = [font.glyph_header(old) for old in order]
        x_min = min(h[1] for h in headers)
        y_min = min(h[2] for h in headers)
        x_max = max(h[3] for h in headers)
        y_max = max(h[4] for h in headers)
        head[36:44] = struct.pack(">hhhh", x_min, y_min, x_max, y_max)
        head = bytes(head)

        # hhea: numberOfHMetrics = numGlyphs, advanceWidthMax real ------------
        hhea = bytearray(_table_bytes(font.tables, font.font_data, "hhea"))
        hhea[10:12] = struct.pack(">H", max(font.advances[old] for old in order))
        hhea[34:36] = struct.pack(">H", num_glyphs)
        hhea = bytes(hhea)

        # maxp: version 0.5 carries only numGlyphs (valid for CJK fonts) ------
        maxp = struct.pack(">IH", 0x00005000, num_glyphs)

        # cmap: (3,1) format 4 + (3,10) format 12, one segment/group per char --
        cmap4 = self._build_cmap4(char_by_new)
        cmap12 = self._build_cmap12(char_by_new)
        cmap_header_len = 4 + 8 * 2
        cmap4_offset = cmap_header_len
        cmap12_offset = cmap_header_len + len(cmap4)
        cmap = (
            struct.pack(">HH", 0, 2)
            + struct.pack(">HHI", 3, 1, cmap4_offset)
            + struct.pack(">HHI", 3, 10, cmap12_offset)
            + cmap4
            + cmap12
        )

        # post: format 3.0 (no glyph names) ----------------------------------
        post = struct.pack(
            ">IihhI",  # version, italicAngle, underlinePosition, thickness
            0x00030000,
            font.italic_angle,
            font.underline_position,
            font.underline_thickness,
            font.is_fixed_pitch,
        ) + b"\x00" * 16  # 32-byte format 3.0 table

        # instruction tables are glyph-independent: keep verbatim when present
        keep_tags = ("cvt ", "fpgm", "prep", "name", "OS/2")
        extra: dict[str, bytes] = {}
        for tag in keep_tags:
            if tag in font.tables:
                extra[tag] = _table_bytes(font.tables, font.font_data, tag)

        tables: dict[str, bytes] = {
            "head": head,
            "hhea": hhea,
            "maxp": maxp,
            "hmtx": bytes(hmtx_new),
            "loca": loca_new,
            "glyf": glyf_new,
            "cmap": cmap,
            "post": post,
        }
        tables.update(extra)

        return self._assemble_sfnt(tables)

    @staticmethod
    def _build_cmap4(chars_by_gid: dict[int, int]) -> bytes:
        """format 4 with one segment per character plus the sentinel."""
        pairs = sorted(
            (code, gid) for gid, code in chars_by_gid.items() if code <= 0xFFFF
        )
        seg_count = len(pairs) + 1
        ends = [code for code, _ in pairs] + [0xFFFF]
        starts = [code for code, _ in pairs] + [0xFFFF]
        deltas = [((gid - code) & 0xFFFF) for code, gid in pairs] + [1]
        range_offsets = [0] * seg_count
        body = struct.pack(">H", seg_count * 2)
        body += struct.pack(f">{seg_count}H", *ends)
        body += b"\x00\x00"  # reservedPad
        body += struct.pack(f">{seg_count}H", *starts)
        body += struct.pack(f">{seg_count}H", *deltas)  # idDelta is u16
        body += struct.pack(f">{seg_count}H", *range_offsets)
        return struct.pack(">HHH", 4, 16 + len(body), 0) + body

    @staticmethod
    def _build_cmap12(chars_by_gid: dict[int, int]) -> bytes:
        """format 12 with one group per character (sorted by code)."""
        pairs = sorted((code, gid) for gid, code in chars_by_gid.items())
        groups = b"".join(
            struct.pack(">III", code, code, gid) for code, gid in pairs
        )
        length = 16 + len(groups)
        return (
            struct.pack(">HHIII", 12, 0, length, 0, len(pairs)) + groups
        )

    @staticmethod
    def _assemble_sfnt(tables: dict[str, bytes]) -> bytes:
        """Assemble a deterministic sfnt from named tables."""
        tags = sorted(tables)
        num_tables = len(tags)
        search_range = 16 * (1 << (num_tables.bit_length() - 1))
        entry_selector = num_tables.bit_length() - 1
        range_shift = num_tables * 16 - search_range

        directory_len = 12 + 16 * num_tables
        offset = directory_len
        records: list[bytes] = []
        data_parts: list[bytes] = []
        for tag in tags:
            body = tables[tag]
            offset = (offset + 3) & ~3  # 4-byte align
            checksum = _checksum(body)
            records.append(struct.pack(">4sIII", tag.encode("latin1"), checksum, offset, len(body)))
            data_parts.append(body + b"\x00" * ((4 - len(body) % 4) % 4))
            offset += len(body)
        header = struct.pack(
            ">IHHHH",
            0x00010000,
            num_tables,
            search_range,
            entry_selector,
            range_shift,
        )
        return header + b"".join(records) + b"".join(data_parts)

    # -- PDF objects -------------------------------------------------------

    def pdf_program_objects(self) -> list[bytes]:
        # Two programs: regular, bold -- in role order.
        bodies: list[bytes] = []
        for program in (self._regular, self._bold):
            font_bytes = self._subset_font(program)
            bodies.append(
                f"<< /Length {len(font_bytes)} /Length1 {len(font_bytes)} >>\nstream\n".encode(
                    "ascii"
                )
                + font_bytes
                + b"\nendstream"
            )
        return bodies

    def pdf_font_objects(
        self, desc_numbers: list[int], touni_numbers: list[int]
    ) -> list[bytes]:
        """Type0 font dictionary bodies in F1..F4 order."""
        bodies: list[bytes] = []
        for index, (face, program) in enumerate(
            zip(_style.FONT_FACES, self._programs_in_face_order())
        ):
            bodies.append(
                (
                    f"<< /Type /Font /Subtype /Type0 /BaseFont /{program.base_font_name}"
                    f" /Encoding /Identity-H /DescendantFonts [{desc_numbers[index]} 0 R]"
                    f" /ToUnicode {touni_numbers[index]} 0 R >>"
                ).encode("ascii")
            )
        return bodies

    def face_descendant_bodies(self) -> list[bytes]:
        """CIDFontType2 bodies in F1..F4 order (descriptor ref injected by
        the writer into ``@@FD@@``)."""
        bodies: list[bytes] = []
        for program in self._programs_in_face_order():
            font = program.font
            upem = font.units_per_em
            order, char_by_new = self._subset_glyphs(program)
            used_new = sorted(char_by_new)
            w_runs: list[str] = []
            index = 0
            while index < len(used_new):
                start = used_new[index]
                run = [start]
                index += 1
                while index < len(used_new) and used_new[index] == run[-1] + 1:
                    run.append(used_new[index])
                    index += 1
                run_widths = [
                    round(font.advances[order[new_gid]] * 1000 / upem)
                    for new_gid in run
                ]
                w_runs.append(f"{start} [{' '.join(map(str, run_widths))}]")
            w_array = " ".join(w_runs)
            bodies.append(
                (
                    f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{program.base_font_name}"
                    f" /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
                    f" /FontDescriptor @@FD@@ 0 R /DW 1000"
                    + (f" /W [{w_array}]" if w_array else "")
                    + " /CIDToGIDMap /Identity >>"
                ).encode("ascii")
            )
        return bodies

    def face_descriptor_bodies(self, prog_numbers: list[int]) -> list[bytes]:
        """FontDescriptor bodies in F1..F4 order.

        ``prog_numbers`` holds [regular, bold]; faces map to their
        program by role (oblique faces share the regular/bold program).
        """
        bodies: list[bytes] = []
        for program in self._programs_in_face_order():
            font = program.font
            x_min, y_min, x_max, y_max = font.bbox
            scale = 1000 / font.units_per_em
            prog_index = 0 if program.role == "regular" else 1
            bodies.append(
                (
                    f"<< /Type /FontDescriptor /FontName /{program.base_font_name}"
                    f" /Flags 4 /FontBBox [{round(x_min * scale)} {round(y_min * scale)}"
                    f" {round(x_max * scale)} {round(y_max * scale)}]"
                    f" /ItalicAngle {font.italic_angle / 65536:.0f}"
                    f" /Ascent {round(font.ascender * scale)}"
                    f" /Descent {round(font.descender * scale)}"
                    f" /CapHeight {round(750 * scale)} /StemV 80"
                    f" /FontFile2 {prog_numbers[prog_index]} 0 R >>"
                ).encode("ascii")
            )
        return bodies

    def pdf_tounicode_objects(self) -> list[bytes]:
        """ToUnicode CMap stream bodies in F1..F4 order."""
        bodies: list[bytes] = []
        for program in self._programs_in_face_order():
            _, char_by_new = self._subset_glyphs(program)
            entries = sorted(
                (gid, code) for gid, code in char_by_new.items()
            )
            # merge consecutive (gid, code) runs into bfranges
            bfchar: list[str] = []
            bfrange: list[str] = []
            index = 0
            while index < len(entries):
                run = [entries[index]]
                index += 1
                while (
                    index < len(entries)
                    and entries[index][0] == run[-1][0] + 1
                    and entries[index][1] == run[-1][1] + 1
                ):
                    run.append(entries[index])
                    index += 1
                if len(run) == 1:
                    gid, code = run[0]
                    bfchar.append(f"<{gid:04X}> <{_utf16be_hex(code)}>")
                else:
                    bfrange.append(
                        f"<{run[0][0]:04X}> <{run[-1][0]:04X}>"
                        f" <{_utf16be_hex(run[0][1])}>"
                    )
            n_bfchar = len(bfchar)
            n_bfrange = len(bfrange)
            cmap = (
                "/CIDInit /ProcSet findresource begin\n"
                "12 dict begin\n"
                "begincmap\n"
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                "/CMapName /Adobe-Identity-UCS def\n"
                "/CMapType 2 def\n"
                "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            )
            if n_bfchar:
                cmap += f"{n_bfchar} beginbfchar\n" + "\n".join(bfchar) + "\nendbfchar\n"
            if n_bfrange:
                cmap += f"{n_bfrange} beginbfrange\n" + "\n".join(bfrange) + "\nendbfrange\n"
            cmap += "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
            raw = cmap.encode("ascii")
            bodies.append(
                f"<< /Length {len(raw)} >>\nstream\n".encode("ascii")
                + raw
                + b"\nendstream"
            )
        return bodies
