"""FDM-201 benchmark report render -- issue #107 acceptance.

The issue's acceptance requires the final reproduction report to render
for the FDM-201 benchmark **and** a real end-to-end project including a
NOT_REPRODUCED-with-closure outcome (the latter is pinned in
``tests/reporting/test_pdf_report.py``). This file reloads the frozen
FDM-201 register into a fresh workspace through the real registry APIs
(the reload convention of ``test_fdm201_reload_audit.py``), freezes the
plan and renders the deterministic PDF report with an injected
``generated_at``:

1. the report renders for the benchmark state -- the frozen 82-item /
   82-requirement inventory, the 20 goals, 10 protocols and 4 closure
   contracts appear in the report (scope/pipeline counts, requirement
   outcome table, closure table, frozen plan refs), the frozen DOI is
   the target identity, and the simulation/real-data label derives from
   the recorded item types (the frozen register has simulation
   categories, so the label is mixed, never silently real-data);
2. the render is deterministic: two fresh workspaces replaying the same
   frozen register produce byte-identical PDF bytes and identical
   section page numbers (same convention as the reload determinism
   tests).

Determinism: fixed identities, fixed timestamps (project init
``2026-01-01``, freeze ``2026-08-14T00:00:00Z``), injected
``generated_at``, no wall clock, no randomness, no network. The system
under test is the real reporting machinery -- never mocked.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_fdm201_reload_audit import (
    DOI,
    FROZEN_AT,
    build_plan_v1,
    execute_reload,
    freeze_plan,
)

from scientific_reproduction.core.models import PlanStatus
from scientific_reproduction.planning.plan import list_plans
from scientific_reproduction.reporting.pdf_report import (
    SECTION_TITLES,
    build_pdf_report,
)

GENERATED_AT = "2026-08-15T00:00:00Z"


def _render(root: Path):
    """Freeze the reloaded plan and render the report (deterministic)."""
    freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    return build_pdf_report(
        root,
        None,
        [],
        generated_at=GENERATED_AT,
    )


def test_fdm201_report_renders_frozen_register_counts(tmp_path: Path) -> None:
    """The report renders for the FDM-201 benchmark state with the frozen
    register counts: 82 items / 82 requirements, 20 goals, 10 protocols,
    4 closure contracts, the frozen DOI and a mixed data label."""
    root = execute_reload(tmp_path)
    report = _render(root)

    data = report.pdf_bytes
    assert data.startswith(b"%PDF-1.4\n")
    assert data.rstrip().endswith(b"%%EOF")
    assert [section.title for section in report.sections] == list(SECTION_TITLES)
    for section in report.sections:
        assert 1 <= section.page_number <= report.pages
    # Target identity (frozen DOI) and scope counts from the registries.
    assert DOI.encode() in data
    assert b"82 items" in data
    assert b"82 requirements" in data
    assert b"20 goals" in data
    assert b"10 protocols" in data
    assert b"4 closure contracts" in data
    # A requirement of the frozen register renders in the outcome table.
    assert b"INV-0301" in data
    # The frozen goal-contract family renders in the governance tables.
    assert b"CC-EXPERIMENT" in data
    # The frozen register contains simulation categories (f), so the
    # label must be mixed -- never silently real-data.
    assert b"mixed" in data
    assert b"computation" in data
    # The frozen plan renders its frozen refs: version, status, the
    # frozen timestamp and the freeze commit SHA (whole, never wrapped
    # mid-token). The registered draft row renders its refs honestly as
    # not frozen / not recorded -- never fabricated.
    plans = list_plans(root)
    frozen = next(plan for plan in plans if plan.status is PlanStatus.FROZEN)
    assert frozen.frozen_commit is not None
    plan_refs = data.split(b"Frozen plan refs:")[1].split(b"Checkpoint events:")[0]
    assert re.search(re.escape(frozen.frozen_commit).encode(), plan_refs) is not None
    assert re.search(rb"[0-9a-f]{40}", plan_refs) is not None
    assert b"FROZEN" in plan_refs
    assert b"2026-08-14T00:00:00Z" in plan_refs
    assert b"v1-draft" in plan_refs
    assert b"not frozen" in plan_refs


def test_fdm201_report_render_is_deterministic_across_workspaces(
    tmp_path: Path,
) -> None:
    """Two fresh workspaces replaying the frozen register render
    byte-identical report PDFs with identical section page numbers."""
    first = _render(execute_reload(tmp_path / "first"))
    second = _render(execute_reload(tmp_path / "second"))

    assert first.pdf_bytes == second.pdf_bytes
    assert first.sections == second.sections
