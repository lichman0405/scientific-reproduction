"""DEV-M11-G03: breakthrough raw/result metadata maps to paper figures/results.

AC-03: a breakthrough template may carry a ``BreakthroughResultTable`` --
representable metadata (result key, paper figure/result reference,
description) recorded on the template, never a worker judgment. The
mapping is deterministic (:func:`paper_mapping` is a pure function of the
table) and is recorded byte-identically in the protocol capture, so
breakthrough raw/result metadata maps to the paper's formal figures/
results across calls. Result keys are safe single registry path segments
(FND-M9-G02-01) and only breakthrough templates may carry the table.

Every test name contains "adsorption" (DEV-M11-G03 naming rule).
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    ANALYSIS_STAGE,
    EXECUTION_STAGE,
    BetTemplate,
    BreakthroughResultTable,
    BreakthroughTemplate,
    InvalidAdsorptionTemplateError,
    InvalidBreakthroughResultError,
    PaperResultEntry,
    capture_protocol,
    paper_mapping,
)


@pytest.fixture
def breakthrough_result_entries() -> tuple[PaperResultEntry, ...]:
    """The raw/result to paper figure/result mapping of a breakthrough run."""
    return (
        PaperResultEntry(
            result_key="breakthrough_time_alkene_a",
            figure_ref="Figure 5",
            description="elution time of the first-eluting component",
        ),
        PaperResultEntry(
            result_key="breakthrough_time_alkene_b",
            figure_ref="Figure 5",
            description="elution time of the second-eluting component",
        ),
        PaperResultEntry(
            result_key="separation_window",
            figure_ref="Figure 6",
            description="retention-time window between the two components",
        ),
        PaperResultEntry(
            result_key="cycle_capacity_retention",
            figure_ref="Table 2",
            description="working capacity retained across regeneration cycles",
        ),
    )


@pytest.fixture
def breakthrough_result_table(
    breakthrough_result_entries: tuple[PaperResultEntry, ...],
) -> BreakthroughResultTable:
    """The recorded results table of a breakthrough analysis template."""
    return BreakthroughResultTable(entries=breakthrough_result_entries)


def test_adsorption_breakthrough_result_entries_are_frozen_records() -> None:
    """Each mapping entry is a frozen, shape-validated record."""
    entry = PaperResultEntry(
        result_key="breakthrough_time_alkene_a",
        figure_ref="Figure 5",
        description="elution time of the first-eluting component",
    )
    assert entry.result_key == "breakthrough_time_alkene_a"
    assert entry.figure_ref == "Figure 5"
    assert entry.description == "elution time of the first-eluting component"
    assert entry.as_dict() == {
        "result_key": "breakthrough_time_alkene_a",
        "figure_ref": "Figure 5",
        "description": "elution time of the first-eluting component",
    }
    with pytest.raises(FrozenInstanceError):
        setattr(entry, "figure_ref", "Figure 9")


def test_adsorption_breakthrough_result_keys_must_be_safe_ids() -> None:
    """Result keys are safe single registry path segments (FND-M9-G02-01)."""
    for result_key in (
        "run/1",
        "run\\1",
        "run 1",
        "run*1",
        "run?1",
        "run[1]",
        ".",
        "..",
        "",
    ):
        with pytest.raises(InvalidBreakthroughResultError):
            PaperResultEntry(
                result_key=result_key,
                figure_ref="Figure 5",
                description="unsafe key",
            )


def test_adsorption_breakthrough_result_table_requires_unique_keys(
    breakthrough_result_entries: tuple[PaperResultEntry, ...],
) -> None:
    """The mapping is one-to-one: result keys are unique and non-empty."""
    with pytest.raises(InvalidBreakthroughResultError):
        BreakthroughResultTable(
            entries=(
                breakthrough_result_entries[0],
                breakthrough_result_entries[0],
            )
        )
    with pytest.raises(TypeError):
        BreakthroughResultTable(entries=())


def test_adsorption_paper_mapping_is_a_pure_sorted_view(
    breakthrough_result_table: BreakthroughResultTable,
) -> None:
    """paper_mapping returns the sorted key -> figure reference mapping."""
    mapping = paper_mapping(breakthrough_result_table)
    assert mapping == {
        "breakthrough_time_alkene_a": "Figure 5",
        "breakthrough_time_alkene_b": "Figure 5",
        "cycle_capacity_retention": "Table 2",
        "separation_window": "Figure 6",
    }
    assert list(mapping) == sorted(mapping)


def test_adsorption_breakthrough_template_carries_results_table(
    breakthrough_result_table: BreakthroughResultTable,
) -> None:
    """AC-03: a breakthrough analysis template records the results table."""
    template = BreakthroughTemplate(
        template_id="breakthrough-2-analysis",
        title="Breakthrough analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "breakthrough_time",
            "criterion": "c_c0_0.5",
            "sampling_validation": "replicate runs with re-packing",
        },
        results=breakthrough_result_table,
    )
    assert template.results is breakthrough_result_table
    assert template.results.entries[0].result_key == "breakthrough_time_alkene_a"


def test_adsorption_only_breakthrough_kind_carries_results_table(
    breakthrough_result_table: BreakthroughResultTable,
) -> None:
    """The results table is a breakthrough contract, not a general shape."""
    with pytest.raises(InvalidAdsorptionTemplateError):
        BetTemplate(
            template_id="bet-with-results",
            title="BET with results",
            stage=EXECUTION_STAGE,
            results=breakthrough_result_table,
        )


def test_adsorption_capture_records_results_mapping_deterministically(
    breakthrough_result_table: BreakthroughResultTable,
) -> None:
    """AC-03: the mapping is captured byte-identically across calls."""
    template = BreakthroughTemplate(
        template_id="breakthrough-3-analysis",
        title="Breakthrough analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "breakthrough_time",
            "criterion": "c_c0_0.5",
            "sampling_validation": "replicate runs with re-packing",
        },
        results=breakthrough_result_table,
    )
    snapshots = {
        json.dumps(capture_protocol(template), sort_keys=True) for _ in range(3)
    }
    assert len(snapshots) == 1
    recorded = capture_protocol(template)["results"]
    assert recorded is not None
    assert recorded["entries"][0] == {
        "result_key": "breakthrough_time_alkene_a",
        "figure_ref": "Figure 5",
        "description": "elution time of the first-eluting component",
    }
    # The captured mapping equals the pure paper_mapping view (AC-03).
    assert {
        entry["result_key"]: entry["figure_ref"]
        for entry in recorded["entries"]
    } == paper_mapping(breakthrough_result_table)


def test_adsorption_breakthrough_mapping_represents_paper_figures(
    breakthrough_result_table: BreakthroughResultTable,
) -> None:
    """AC-03: every mapped reference is representable, recorded metadata."""
    for entry in breakthrough_result_table.entries:
        assert entry.figure_ref
        assert entry.description
        # Representable metadata: an explicit paper figure/result reference,
        # never a computed or judged value.
        assert entry.figure_ref.startswith(("Figure", "Table"))
    mapping = paper_mapping(breakthrough_result_table)
    assert "separation_window" in mapping


def test_adsorption_results_table_type_boundaries_raise_type_error(
    breakthrough_result_table: BreakthroughResultTable,
) -> None:
    """Wrong table/entry types are TypeError at the boundary."""
    bad_results: Any = {"entries": []}
    with pytest.raises(TypeError):
        BreakthroughTemplate(
            template_id="breakthrough-bad-results",
            title="Bad results",
            stage=ANALYSIS_STAGE,
            results=bad_results,
        )
    bad_entries: Any = ({"result_key": "x"},)
    with pytest.raises(TypeError):
        BreakthroughResultTable(entries=bad_entries)
    bad_key: Any = 7
    with pytest.raises(TypeError):
        PaperResultEntry(
            result_key=bad_key,
            figure_ref="Figure 5",
            description="bad key",
        )
    with pytest.raises(TypeError):
        paper_mapping("not-a-table")
