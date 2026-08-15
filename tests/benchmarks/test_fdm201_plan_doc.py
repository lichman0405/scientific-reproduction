"""Issue #105 acceptance: the frozen FDM-201 benchmark renders into a
designed plan document.

The full frozen FDM-201 state (82/82 inventory, 20-goal contract family)
is reloaded through the real registration APIs, frozen through the real
audit gate (``freeze_plan``, fixed ``FROZEN_AT`` stamp) and rendered
with a fixed injected ``generated_at`` -- the document must cover the
cover identity, the inventory coverage summary, the 82-requirement
table, the 20-node goal DAG diagram and one section per goal, render
byte-identically on repeated runs, and persist under ``reports/`` with a
machine-verifiable SHA-256 checksum. The reload registers no statistical
designs, so the SS8 "no basis on record" path is exercised at benchmark
scale.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from test_fdm201_reload_audit import (
    DOI,
    FROZEN_AT,
    FROZEN_PROJECT_ID,
    execute_reload,
)

from scientific_reproduction.planning.freeze import freeze_plan
from scientific_reproduction.planning.plan import build_plan_v1
from scientific_reproduction.reporting.plan_doc import (
    render_plan_document,
    write_plan_document,
)

#: Fixed generation timestamp injected into the render (no wall clock).
GENERATED_AT = datetime(2026, 8, 15, tzinfo=timezone.utc)


def install_frozen_fdm201(root: Path) -> Path:
    """Reload the full frozen FDM-201 state and freeze Plan v1 (fixed
    stamp); return ``root``."""
    root = execute_reload(root)
    freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    return root


def test_fdm201_plan_doc_renders_the_frozen_plan(tmp_path: Path) -> None:
    """The renderer handles the full benchmark workspace: cover identity
    and freeze stamp, coverage summary, 82-requirement table, 20-node
    DAG and one section per goal (issue #105 acceptance)."""
    root = install_frozen_fdm201(tmp_path)
    doc = render_plan_document(root, "v1", generated_at=GENERATED_AT)
    text = doc.to_html()

    # Cover: identity, primary target, freeze stamp, domain pack.
    assert doc.project_id == FROZEN_PROJECT_ID
    assert doc.primary_target == DOI
    assert doc.plan_version == "v1"
    assert doc.plan_status == "FROZEN"
    assert doc.frozen_at == "2026-08-14T00:00:00Z"
    assert doc.frozen_commit
    assert doc.domain_pack == "materials-chemistry"
    assert FROZEN_PROJECT_ID in text
    assert DOI in text
    assert "2026-08-14T00:00:00Z" in text

    # Sections: the fixed trio plus one section per plan goal. The plan
    # covers the 18 item-mapped goals (the reload audit test asserts the
    # same 18-goal plan).
    titles = [section.title for section in doc.sections]
    assert titles[:3] == ["Scope declaration", "Requirements", "Goal DAG"]
    assert len(titles) == 21
    assert all(title.startswith("Goal GOAL-") for title in titles[3:])
    assert len(set(titles[3:])) == 18
    plan_goal_ids = [title[len("Goal "):] for title in titles[3:]]

    # Scope: the frozen 82/82 PASS coverage, recomputed at render time.
    scope = doc.section("Scope declaration")
    assert scope is not None
    assert "<td>82</td>" in scope.body
    assert "1.00" in scope.body
    assert "Recomputed verdict: PASS" in scope.body

    # Requirements: 82 rows plus the header row.
    requirements = doc.section("Requirements")
    assert requirements is not None
    assert requirements.body.count("<tr>") == 83
    # The frozen mapping mirrors requirement_id == item_id (INV-* ids).
    assert 'class="mono">INV-' in requirements.body
    assert "CRITICAL" in requirements.body

    # DAG: the deterministic SVG carries every plan goal node.
    dag = doc.section("Goal DAG")
    assert dag is not None
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in dag.body
    for goal_id in plan_goal_ids:
        assert goal_id in dag.body
    assert dag.body.count('class="dag-id"') >= 18

    # Per-goal sections render the frozen records (design + closure).
    goal = doc.section(f"Goal {plan_goal_ids[0]}")
    assert goal is not None
    assert "Analysis protocol summary" in goal.body
    assert "Closure contract summary" in goal.body
    # No statistical designs are registered: the SS8 provenance is
    # surfaced explicitly, never invented.
    assert "no basis on record (07 SS8)" in goal.body
    assert "No statistical design record" in goal.body


def test_fdm201_plan_doc_byte_identical_double_render(tmp_path: Path) -> None:
    """Repeated renders of the full benchmark are byte-identical
    (issue #105 determinism acceptance)."""
    root = install_frozen_fdm201(tmp_path)
    first = render_plan_document(root, "v1", generated_at=GENERATED_AT).to_html()
    second = render_plan_document(root, "v1", generated_at=GENERATED_AT).to_html()

    assert first == second


def test_fdm201_plan_doc_write_reports_with_checksum(tmp_path: Path) -> None:
    """The benchmark document persists under ``reports/`` with a
    machine-verifiable SHA-256 checksum sidecar (issue #105
    acceptance)."""
    root = install_frozen_fdm201(tmp_path)
    result = write_plan_document(root, "v1", generated_at=GENERATED_AT)

    assert result.html_path == tmp_path / "reports" / "plan-v1.html"
    html_text = result.html_path.read_text(encoding="utf-8")
    checksum = result.checksum_path.read_text(encoding="utf-8")
    assert checksum == f"{result.sha256}  plan-v1.html\n"
    assert hashlib.sha256(html_text.encode("utf-8")).hexdigest() == result.sha256
    # The stored bytes are exactly the document's deterministic HTML.
    assert html_text == result.document.to_html()
