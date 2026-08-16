"""Language-pack tests (issue #122).

The template packs are the explicit-language mechanism of the rendered
outputs (execution sheets, sheet PDFs, the reproduction report PDF):
the renderers take a ``language`` key (default ``"en"``) and resolve it
through :func:`resolve_pack` -- there is no locale auto-detection, so
``(state, language)`` maps to byte-identical output. These tests pin
the pack surface:

* ``available`` -- the shipped language keys and the resolution table;
* ``parity`` -- every pack of every language carries the same keys
  (a missing key is a pack bug, not a render-time surprise);
* ``boundaries`` -- ``TypeError`` / ``ValueError`` with stable messages;
* ``defaults`` -- the English pack reproduces the pre-pack renderer
  strings verbatim (the byte-identity guarantee of the renderers).
"""

from __future__ import annotations

import dataclasses

import pytest

from scientific_reproduction.reporting.language import (
    AVAILABLE_LANGUAGES,
    EN_PACK,
    ZH_PACK,
    TemplatePack,
    resolve_pack,
)


def _pack_keys(pack: TemplatePack) -> dict[type, frozenset[str]]:
    """The field names of each pack dataclass of one language."""
    return {
        type(value): frozenset(field.name for field in dataclasses.fields(value))
        for value in (pack, pack.experiment, pack.computation, pack.report)
    }


# ---------------------------------------------------------------------------
# available languages and the resolution table
# ---------------------------------------------------------------------------


def test_language_available_languages_are_en_and_zh() -> None:
    assert AVAILABLE_LANGUAGES == ("en", "zh")


def test_language_resolve_pack_returns_the_shipped_packs() -> None:
    assert resolve_pack("en") is EN_PACK
    assert resolve_pack("zh") is ZH_PACK


# ---------------------------------------------------------------------------
# parity: every pack carries the same keys
# ---------------------------------------------------------------------------


def test_language_pack_parity_between_languages() -> None:
    # A language that misses a field would render a different structure
    # -- parity pins every pack to the same key set.
    assert _pack_keys(EN_PACK) == _pack_keys(ZH_PACK)


def test_language_english_defaults_match_the_prerelease_strings() -> None:
    # The English pack reproduces the pre-pack renderers verbatim (the
    # byte-identity guarantee of the sheet/report renderers).
    assert EN_PACK.language == "en"
    assert EN_PACK.html_lang == "en"
    assert EN_PACK.not_recorded == "not recorded"
    assert EN_PACK.not_registered == "not registered in the project registry"
    assert EN_PACK.deterministic_render == "deterministic render"
    assert EN_PACK.experiment.title == "Experiment Execution Sheet"
    assert EN_PACK.experiment.section_procedure == "Procedure"
    assert EN_PACK.report.title == "Reproduction Report"
    assert EN_PACK.report.section_titles[0] == "Executive summary"


def test_language_chinese_pack_ships_translations() -> None:
    # The zh pack translates the renderer template strings (manifest
    # content -- reagent names, step titles, schema keys -- is data and
    # is never translated).
    assert ZH_PACK.language == "zh"
    assert ZH_PACK.html_lang == "zh"
    assert ZH_PACK.not_recorded == "未记录"
    assert ZH_PACK.experiment.title == "实验执行单"
    assert ZH_PACK.computation.title == "计算执行单"
    assert ZH_PACK.report.title == "复现报告"
    assert ZH_PACK.report.section_titles == (
        "执行摘要",
        "目标论文身份与复现范围",
        "流水线摘要",
        "需求结果",
        "核心发现",
        "治理行使",
        "审计追踪",
        "模拟与真实数据标注",
    )


# ---------------------------------------------------------------------------
# boundaries: stable TypeError / ValueError
# ---------------------------------------------------------------------------


def test_language_resolve_pack_unknown_language_raises() -> None:
    with pytest.raises(ValueError, match="unknown render language 'fr'"):
        resolve_pack("fr")
    with pytest.raises(ValueError, match="available languages: en, zh"):
        resolve_pack("fr")


def test_language_resolve_pack_type_error_boundaries() -> None:
    with pytest.raises(TypeError, match="language must be a non-empty string"):
        resolve_pack("")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="language must be a non-empty string"):
        resolve_pack(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="language must be a non-empty string"):
        resolve_pack(None)  # type: ignore[arg-type]
