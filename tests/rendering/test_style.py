"""Visual-system token tests (issue #107 rendering foundation).

``rendering.style`` is the single source of the shared visual tokens the
reproduction report, plan and execution-sheet renderers use; these tests
pin the verdict color mapping and the AFM text measurement contract.
"""
from __future__ import annotations

import pytest

from scientific_reproduction.rendering.style import (
    FAIL_BG,
    FAIL_COLOR,
    FONT_BODY,
    FONT_BOLD,
    INCONCLUSIVE_BG,
    INCONCLUSIVE_COLOR,
    NEUTRAL_BG,
    NEUTRAL_COLOR,
    PASS_BG,
    PASS_COLOR,
    text_width,
    verdict_style,
)


def test_style_verdict_mapping_is_case_insensitive() -> None:
    """The frozen verdicts map to their color pairs case-insensitively."""
    assert verdict_style("PASS") == (PASS_COLOR, PASS_BG)
    assert verdict_style("pass") == (PASS_COLOR, PASS_BG)
    assert verdict_style("FAIL") == (FAIL_COLOR, FAIL_BG)
    assert verdict_style("INCONCLUSIVE") == (INCONCLUSIVE_COLOR, INCONCLUSIVE_BG)


def test_style_unknown_verdict_falls_back_to_neutral() -> None:
    """Unknown or empty verdicts render neutral, never green or red."""
    assert verdict_style("SOMETHING_ELSE") == (NEUTRAL_COLOR, NEUTRAL_BG)
    assert verdict_style("") == (NEUTRAL_COLOR, NEUTRAL_BG)


def test_style_text_width_uses_afm_ascii_widths() -> None:
    """Text measurement follows the standard Helvetica AFM widths."""
    assert text_width(" ", FONT_BODY, 10) == pytest.approx(2.78)
    assert text_width("i", FONT_BODY, 10) == pytest.approx(2.22)
    assert text_width("m", FONT_BODY, 10) == pytest.approx(8.33)
    assert text_width("W", FONT_BODY, 10) == pytest.approx(9.44)
    assert text_width("W", FONT_BOLD, 10) == pytest.approx(9.44)
    # H=0.722 e=0.556 l=0.222 l=0.222 o=0.556 em -> 22.78 pt at 10 pt
    assert text_width("Hello", FONT_BODY, 10) == pytest.approx(22.78)


def test_style_text_width_scales_with_size() -> None:
    """Width is linear in the font size."""
    assert text_width("W", FONT_BODY, 20) == pytest.approx(18.88)
    assert text_width("Hello", FONT_BODY, 20) == pytest.approx(45.56)


def test_style_text_width_fallback_is_deterministic() -> None:
    """Characters outside the tables (e.g. ≤) fall back to a fixed
    positive width; unknown faces fall back to the Helvetica table."""
    assert text_width("≤", FONT_BODY, 10) > 0
    assert text_width("≤", FONT_BODY, 10) == text_width("≤", FONT_BODY, 10)
    assert text_width("W", "Times-Roman", 10) == text_width(
        "W", FONT_BODY, 10
    )
