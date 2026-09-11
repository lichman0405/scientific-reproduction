"""Unicode font backend regression tests.

The design contract (see ``rendering/fonts.py``):

* the Unicode backend preserves arbitrary Unicode text -- CJK, Greek and
  technical symbols render and extract round-trip, never as ``?``;
* characters missing from the embedded font surface a warning and draw
  as ``.notdef`` -- never silently replaced;
* repeated renders of the same draw sequence are byte-identical
  (deterministic subsetting);
* layout measurement uses the embedded font's real advances;
* the legacy base-14 path keeps its historical bytes (cp1252, ``?``
  fallback) for the low-level writer, and ``strict=True`` raises instead
  of degrading, for product paths that must never lose text.
"""

from __future__ import annotations

import io

import pytest

from scientific_reproduction.rendering.fonts import (
    Base14Backend,
    FontConfig,
    TrueTypeBackend,
)
from scientific_reproduction.rendering.pdf import PdfDocument
from scientific_reproduction.rendering.style import (
    FONT_BODY,
    FONT_BOLD,
    measure_backend,
    text_width,
)

pypdf = pytest.importorskip("pypdf")

CHINESE_SENTENCE = "断裂强度随 CS 含量增加而增大；40%"
MIXED_SYMBOLS = "中文 + English + 123 + μm + ℃ + ± + σ + × + → + ²"


def _render_unicode(lines: list[tuple[str, str]]) -> tuple[bytes, TrueTypeBackend]:
    """Render ``lines`` of (text, font) through the Unicode backend."""
    backend = TrueTypeBackend(FontConfig.default())
    doc = PdfDocument(title="fixture", font_backend=backend)
    page = doc.add_page()
    page.set_font(FONT_BODY, 10)
    for text, font in lines:
        page.set_font(font, 10)
        page.text(72, 720, text)
    return doc.render(), backend


def _extract(data: bytes) -> str:
    reader = pypdf.PdfReader(io.BytesIO(data))
    return (reader.pages[0].extract_text() or "").strip()


def test_unicode_backend_renders_chinese_roundtrip() -> None:
    """Chinese text survives render -> extract exactly."""
    data, backend = _render_unicode([(CHINESE_SENTENCE, FONT_BODY)])
    assert backend.warnings() == []
    assert _extract(data) == CHINESE_SENTENCE


def test_unicode_backend_mixed_text_and_symbols() -> None:
    """CJK + Latin + digits + Greek + technical symbols all round-trip."""
    data, backend = _render_unicode([(MIXED_SYMBOLS, FONT_BODY)])
    assert backend.warnings() == []
    assert _extract(data) == MIXED_SYMBOLS


def test_unicode_backend_no_question_mark_replacement() -> None:
    """Unmappable characters are never silently replaced by '?'."""
    data, backend = _render_unicode(
        [(CHINESE_SENTENCE, FONT_BODY), (MIXED_SYMBOLS, FONT_BOLD)]
    )
    extracted = _extract(data)
    assert "?" not in extracted
    assert backend.warnings() == []


def test_unicode_backend_synthetic_fallback_roundtrip() -> None:
    """A character missing from the font but with an NFKD base (the
    superscript minus of ``cm⁻¹``) draws the base glyph under its own
    subset gid while ToUnicode keeps the original character: the text
    round-trips exactly, and the substitution is surfaced as a warning
    -- never silent, never ``?``."""
    text = "cm⁻¹ 峰值 3436 cm⁻¹"
    data, backend = _render_unicode([(text, FONT_BODY)])
    extracted = _extract(data)
    assert extracted == text  # extraction keeps the original character
    assert "?" not in extracted
    warnings = backend.warnings()
    assert len(warnings) == 1
    assert "U+207B" in warnings[0]
    assert ".notdef" not in warnings[0]  # drawn from the base glyph


def test_unicode_backend_missing_glyph_warns_and_uses_notdef() -> None:
    """A character outside the font's cmap warns and draws .notdef."""
    data, backend = _render_unicode([("测试😀emoji", FONT_BODY)])
    warnings = backend.warnings()
    assert len(warnings) == 1
    assert "U+1F600" in warnings[0]
    extracted = _extract(data)
    # The emoji is dropped (notdef), everything else survives; '?' never
    # appears anywhere in the extracted text.
    assert "?" not in extracted
    assert extracted.startswith("测试")


def test_unicode_backend_deterministic_bytes() -> None:
    """Same draw sequence -> byte-identical PDFs."""
    backend = TrueTypeBackend(FontConfig.default())
    doc = PdfDocument(title="fixture", font_backend=backend)
    page = doc.add_page()
    page.set_font(FONT_BODY, 10)
    page.text(72, 720, CHINESE_SENTENCE)
    page.set_font(FONT_BOLD, 12)
    page.text(72, 700, MIXED_SYMBOLS)
    assert doc.render() == doc.render()


def test_unicode_backend_measure_uses_real_advances() -> None:
    """Measurement uses the embedded font's advances, so layout matches
    rendering (CJK advance = 1.0 em in the bundled font)."""
    backend = TrueTypeBackend(FontConfig.default())
    with measure_backend(backend):
        assert text_width("断", FONT_BODY, 10) == pytest.approx(10.0)
        assert text_width("断", FONT_BOLD, 12) == pytest.approx(12.0)
    # outside the context, the AFM tables apply: characters cp1252
    # cannot draw (CJK, ...) fall back to a full em (1.0) --
    # under-measuring CJK made layout skip wraps and overflow table
    # cells -- while unknown ASCII controls keep the narrow generic
    # fallback (0.556).
    assert text_width("断", FONT_BODY, 10) == pytest.approx(10.0)
    assert text_width("", FONT_BODY, 10) == pytest.approx(5.56)


def test_afm_fallback_never_under_measures() -> None:
    """AFM estimates never fall below what a character can render as.

    P16b: the table is keyed by real characters (byte slots could never
    be hit by raw-text measurement, under-measuring the em dash 1.8x),
    and characters cp1252 cannot draw are conservatively a full em --
    only cp1252-drawable characters the table omits keep the 0.556
    approximation.
    """
    assert text_width("—", FONT_BODY, 10) == pytest.approx(10.0)  # table key
    assert text_width("“", FONT_BODY, 10) == pytest.approx(3.33)  # table key
    assert text_width("≤", FONT_BODY, 10) == pytest.approx(10.0)  # non-cp1252
    assert text_width("Ａ", FONT_BODY, 10) == pytest.approx(10.0)  # fullwidth
    assert text_width("æ", FONT_BODY, 10) == pytest.approx(5.56)  # cp1252 tier


def test_base14_backend_legacy_bytes_preserved() -> None:
    """The low-level base-14 path keeps its historical WinAnsi bytes."""
    doc = PdfDocument(title="fixture")
    page = doc.add_page()
    page.set_font(FONT_BODY, 10)
    page.text(72, 720, "café ☃")
    data = doc.render()
    assert b"caf\\351" in data  # é -> cp1252 0xE9, octal-escaped
    assert b"?" in data  # snowman has no cp1252 byte: '?' fallback


def test_base14_backend_strict_raises_instead_of_degrading() -> None:
    """strict=True refuses text the base-14 path cannot represent."""
    backend = Base14Backend(strict=True)
    with pytest.raises(ValueError):
        backend.encode_text("断裂", FONT_BODY)
    # cp1252-representable text still passes
    assert backend.encode_text("café", FONT_BODY) == b"(caf\\351)"


def test_unicode_backend_font_config_missing_raises(monkeypatch) -> None:
    """A missing font directory raises loudly, never falls back to '?'."""
    monkeypatch.setenv("SCIENTIFIC_REPRODUCTION_FONT_DIR", "/nonexistent/dir")
    with pytest.raises(ValueError):
        FontConfig.default()


# --- System font discovery (bundling-free rendering) ------------------------

def test_default_uses_system_fonts_when_bundled_missing(monkeypatch):
    """Without env override, default() prefers host system CJK fonts over
    the bundled assets dir (referenced, never redistributed)."""
    from scientific_reproduction.rendering import fonts as _fonts

    monkeypatch.delenv(_fonts.ENV_FONT_DIR, raising=False)
    cand = _fonts._system_font_candidates()
    if not cand:
        pytest.skip("no system CJK fonts on this host")
    cfg = _fonts.FontConfig.default()
    sys_dirs = {p1.parent for p1, p2 in cand}
    assert cfg.regular.parent in sys_dirs
    assert "assets" not in str(cfg.regular).lower()


def test_env_override_wins_over_system_fonts(monkeypatch, tmp_path):
    """An explicit SCIENTIFIC_REPRODUCTION_FONT_DIR must beat system fonts."""
    from scientific_reproduction.rendering import fonts as _fonts

    font_dir = tmp_path / "fonts"
    font_dir.mkdir()
    (font_dir / _fonts.REGULAR_FILE).write_bytes(b"regular")
    (font_dir / _fonts.BOLD_FILE).write_bytes(b"bold")
    monkeypatch.setenv(_fonts.ENV_FONT_DIR, str(font_dir))
    cfg = _fonts.FontConfig.default()
    assert cfg.regular == font_dir / _fonts.REGULAR_FILE
    assert cfg.bold == font_dir / _fonts.BOLD_FILE


def test_missing_system_and_bundled_fonts_raise(monkeypatch, tmp_path):
    """No env, no system fonts, no bundled fonts -> FontConfigError with hint."""
    from scientific_reproduction.rendering import fonts as _fonts

    monkeypatch.delenv(_fonts.ENV_FONT_DIR, raising=False)
    monkeypatch.setattr(_fonts, "_system_font_candidates", lambda: [])
    # Force bundled dir to an empty temp location.
    empty = tmp_path / "assets" / "fonts"
    empty.mkdir(parents=True)
    monkeypatch.setattr(_fonts, "__file__", str(empty / "fonts.py"))
    with pytest.raises(_fonts.FontConfigError, match=r"SCIENTIFIC_REPRODUCTION_FONT_DIR"):
        _fonts.FontConfig.default()
