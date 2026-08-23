"""Per-goal revision lineage (issue #164).

Every test name contains "goal_revision" so ``python -m pytest -q
tests/planning -k goal_revision`` selects the whole suite. The sections
map to the acceptance criteria of issue #164:

* ``revise_goal`` -- reopens ONE registered FROZEN goal as a draft of
  the next version with ``parent_goal_id`` set (goal-level lineage);
  sibling goals -- and the acceptance / statistical-design / analysis /
  closure records -- stay frozen and byte-untouched;
* ``revise_plan`` subset -- the explicit ``goal_subset`` reopens only
  the submitted goals whose content changed (a submitted contract equal
  to the registered frozen record is left frozen); the whole-family path
  (no subset) keeps its plan-wide revision behavior and sets
  ``parent_goal_id`` on every reopened goal;
* ``lineage`` -- the revision is visible through the existing records:
  ``parent_goal_id`` on the reopened goal, ``parent_plan_version`` on
  the revised plan, and the plan lineage supersession view.

The deterministic path mirrors ``test_freeze``: fixtures use the fixed
identities/timestamps of that suite, and helpers are imported read-only
from ``test_freeze`` (``build_complete_workspace``,
``_register_mapped_goal``, ``freeze_complete``).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_freeze import (
    FROZEN_AT,
    _register_mapped_goal,
    build_complete_workspace,
    freeze_complete,
    make_goal,
)

from scientific_reproduction.core.models import GoalContract, PlanStatus
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.freeze import (
    GoalNotFrozenError,
    GoalStateMismatchError,
    freeze_plan,
    revise_goal,
    revise_plan,
)
from scientific_reproduction.planning.init import ProjectNotInitializedError
from scientific_reproduction.planning.plan import (
    GoalNotFoundError,
    plan_lineage,
    read_goal,
    read_plan,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def build_two_goal_workspace(root: Path) -> Path:
    """A freeze-eligible workspace with two mapped goals (GOAL-1, GOAL-2).

    GOAL-2 shares the registered family records of GOAL-1 (acceptance
    ``ACC-1``, analysis ``ANL-1``, closure ``CLS-1``) through the
    ``_register_mapped_goal`` helper of ``test_freeze`` (issue #142), so
    both goals freeze together into one plan.
    """
    build_complete_workspace(root)
    _register_mapped_goal(
        root, "GOAL-2", item_id="ITEM-3", requirement_id="REQ-3"
    )
    return root


def freeze_two_goals(root: Path):
    """Freeze the two-goal workspace deterministically (fixed timestamp)."""
    return freeze_complete(root)


def _bytes(path: Path) -> bytes:
    return path.read_bytes()


def _frozen_goal(root: Path, goal_id: str) -> GoalContract:
    """Read a goal and assert it is the frozen contract (test sanity)."""
    goal = read_goal(root, goal_id)
    assert goal.frozen is True
    return goal


# ---------------------------------------------------------------------------
# revise_goal: one goal reopened, siblings frozen and byte-untouched
# ---------------------------------------------------------------------------


def test_goal_revision_revise_goal_reopens_one_goal_with_lineage(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    freeze_two_goals(root)
    frozen1 = _frozen_goal(root, "GOAL-1")
    amended = replace(
        frozen1,
        objective="Amended: repeat the isotherm at 298 K instead of 273 K.",
    )

    draft = revise_goal(root, amended)

    # The next draft version, mirroring how revise_plan versions the
    # family (v1 -> v2-draft), with the goal-level lineage marker set.
    assert draft.goal_id == "GOAL-1"
    assert draft.version == "v2-draft"
    assert draft.frozen is False
    assert draft.parent_goal_id == "GOAL-1"
    assert draft.frozen_at is None
    assert draft.frozen_commit is None
    assert draft.acceptance.frozen is False
    # The submitted amendment is the authoring baseline.
    assert draft.objective == amended.objective
    assert draft.title == frozen1.title
    # The record is persisted in place: readers see the reopened draft.
    assert read_goal(root, "GOAL-1") == draft
    # The sibling goal stays FROZEN at the frozen version.
    sibling = _frozen_goal(root, "GOAL-2")
    assert sibling.version == "v1"
    assert sibling.parent_goal_id is None


def test_goal_revision_revise_goal_siblings_and_family_byte_untouched(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    freeze_two_goals(root)
    before = {
        path: _bytes(root / path)
        for path in (
            Path("goals") / "GOAL-1.json",
            Path("goals") / "GOAL-2.json",
            Path("acceptance") / "ACC-1.json",
            Path("protocols") / "ANL-1.json",
            Path("closure") / "CLS-1.json",
            Path("designs") / "DESIGN-1.json",
        )
    }

    revise_goal(
        root,
        replace(
            _frozen_goal(root, "GOAL-1"),
            objective="Amended: repeat the isotherm at 298 K instead of 273 K.",
        ),
    )

    # Only the revised goal's own record is rewritten; every sibling goal
    # and every other goal-family record stays byte-identical.
    for path, original in before.items():
        current = _bytes(root / path)
        if path == Path("goals") / "GOAL-1.json":
            assert current != original
        else:
            assert current == original, f"{path} was rewritten by revise_goal"


def test_goal_revision_revise_goal_rejects_unregistered_goal(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    freeze_two_goals(root)
    with pytest.raises(GoalNotFoundError) as exc:
        revise_goal(root, make_goal("GOAL-9"))
    assert "GOAL-9" in str(exc.value)
    # Nothing is written: the family stays frozen.
    assert _frozen_goal(root, "GOAL-1").version == "v1"


def test_goal_revision_revise_goal_requires_frozen_goal(tmp_path):
    # A draft family record is not revisable: revision reopens frozen
    # records only.
    root = build_two_goal_workspace(tmp_path)
    draft = read_goal(root, "GOAL-1")
    assert draft.frozen is False
    with pytest.raises(GoalNotFrozenError) as exc:
        revise_goal(root, draft)
    assert "not frozen" in str(exc.value)


def test_goal_revision_revise_goal_rejects_double_revision(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    freeze_two_goals(root)
    revised = revise_goal(
        root, replace(_frozen_goal(root, "GOAL-1"), objective="Amended.")
    )
    with pytest.raises(GoalNotFrozenError):
        revise_goal(root, replace(revised, objective="Amended again."))
    assert read_goal(root, "GOAL-1") == revised


def test_goal_revision_revise_goal_rejects_stale_goal_object(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    freeze_two_goals(root)
    stale = replace(_frozen_goal(root, "GOAL-1"), version="v1-draft")
    messages = []
    for _ in range(2):
        with pytest.raises(GoalStateMismatchError) as exc:
            revise_goal(root, stale)
        messages.append(str(exc.value))
    # Stable error naming the goal and both versions.
    assert messages[0] == messages[1]
    assert "GOAL-1" in messages[0]
    assert "v1-draft" in messages[0]
    assert "v1" in messages[0]
    # Nothing is written: the registered record stays frozen.
    assert _frozen_goal(root, "GOAL-1").version == "v1"


def test_goal_revision_revise_goal_type_error_boundaries(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    freeze_two_goals(root)
    with pytest.raises(TypeError):
        revise_goal(123, _frozen_goal(root, "GOAL-1"))  # type: ignore
    with pytest.raises(TypeError):
        revise_goal(root, "not-a-goal")  # type: ignore
    with pytest.raises(TypeError):
        revise_goal(root, {"goal_id": "GOAL-1"})  # type: ignore


def test_goal_revision_revise_goal_requires_initialized_project(tmp_path):
    bare = tmp_path / "bare"
    with pytest.raises(ProjectNotInitializedError):
        revise_goal(bare, make_goal("GOAL-1"))


def test_goal_revision_revise_goal_schema_valid_and_persisted(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    freeze_two_goals(root)
    draft = revise_goal(
        root, replace(_frozen_goal(root, "GOAL-1"), objective="Amended.")
    )
    validate_and_reject("goal", draft.to_dict())
    raw = json.loads((root / "goals" / "GOAL-1.json").read_text(encoding="utf-8"))
    assert raw["version"] == "v2-draft"
    assert raw["frozen"] is False
    assert raw["parent_goal_id"] == "GOAL-1"
    # Unset optional fields serialize as absent keys (CoreModel.to_dict).
    assert raw.get("frozen_at") is None
    assert raw.get("frozen_commit") is None


def test_goal_revision_revise_goal_deterministic_across_workspaces(tmp_path):
    root_a = build_two_goal_workspace(tmp_path / "a")
    root_b = build_two_goal_workspace(tmp_path / "b")
    freeze_two_goals(root_a)
    freeze_two_goals(root_b)
    draft_a = revise_goal(
        root_a, replace(_frozen_goal(root_a, "GOAL-1"), objective="Amended.")
    )
    draft_b = revise_goal(
        root_b, replace(_frozen_goal(root_b, "GOAL-1"), objective="Amended.")
    )
    assert draft_a == draft_b
    assert draft_a.to_dict() == draft_b.to_dict()


def test_goal_revision_next_draft_version_mirrors_family_versioning(tmp_path):
    # v1 -> v2-draft -> (freeze) v2 -> v3-draft: the next goal draft
    # version mirrors how revise_plan versions the goal family.
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan
    v2_draft = revise_plan(root, v1)  # whole family reopened at v2-draft
    v2 = freeze_plan(root, v2_draft, timestamp=FROZEN_AT).frozen_plan
    assert v2.version == "v2"
    assert _frozen_goal(root, "GOAL-1").version == "v2"

    draft = revise_goal(
        root, replace(_frozen_goal(root, "GOAL-1"), objective="Third amendment.")
    )
    assert draft.version == "v3-draft"
    assert draft.parent_goal_id == "GOAL-1"


# ---------------------------------------------------------------------------
# revise_plan goal subset: only changed goals are reopened
# ---------------------------------------------------------------------------


def test_goal_revision_revise_plan_subset_reopens_only_changed_goals(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan
    amended = replace(
        _frozen_goal(root, "GOAL-1"),
        objective="Amended: repeat the isotherm at 298 K instead of 273 K.",
    )
    unchanged = _frozen_goal(root, "GOAL-2")  # exactly the frozen record
    sibling_before = _bytes(root / "goals" / "GOAL-2.json")

    revised = revise_plan(root, v1, goal_subset=(amended, unchanged))

    assert revised.version == "v2-draft"
    assert revised.parent_plan_version == "v1"
    assert set(revised.goal_ids) == {"GOAL-1", "GOAL-2"}
    # The changed goal is reopened as the next draft with lineage set.
    changed = read_goal(root, "GOAL-1")
    assert changed.frozen is False
    assert changed.version == "v2-draft"
    assert changed.parent_goal_id == "GOAL-1"
    assert changed.objective == amended.objective
    # The unchanged subset member is NOT reopened: content equal to the
    # registered frozen record means nothing changed.
    sibling = read_goal(root, "GOAL-2")
    assert sibling == unchanged
    assert sibling.frozen is True
    assert sibling.version == "v1"
    assert _bytes(root / "goals" / "GOAL-2.json") == sibling_before
    # The old plan record stays byte-untouched.
    assert read_plan(root, "v1") == v1


def test_goal_revision_revise_plan_subset_leaves_other_family_records(
    tmp_path,
):
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan
    before = {
        path: _bytes(root / path)
        for path in (
            Path("goals") / "GOAL-2.json",
            Path("acceptance") / "ACC-1.json",
            Path("protocols") / "ANL-1.json",
            Path("closure") / "CLS-1.json",
            Path("designs") / "DESIGN-1.json",
        )
    }

    revise_plan(
        root,
        v1,
        goal_subset=(
            replace(_frozen_goal(root, "GOAL-1"), objective="Amended."),
        ),
    )

    # The subset path rewrites only the submitted changed goal records:
    # goals outside the subset and the acceptance / design / analysis /
    # closure records stay frozen and byte-identical.
    for path, original in before.items():
        assert _bytes(root / path) == original, (
            f"{path} was rewritten by the subset revision"
        )
    assert read_goal(root, "GOAL-2").frozen is True
    assert read_goal(root, "GOAL-1").version == "v2-draft"


def test_goal_revision_revise_plan_empty_subset_revises_plan_only(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan
    goal1_before = _bytes(root / "goals" / "GOAL-1.json")
    goal2_before = _bytes(root / "goals" / "GOAL-2.json")

    revised = revise_plan(root, v1, goal_subset=())

    assert revised.version == "v2-draft"
    assert revised.parent_plan_version == "v1"
    # No goal is reopened: the family stays frozen and byte-identical.
    assert _frozen_goal(root, "GOAL-1").version == "v1"
    assert _frozen_goal(root, "GOAL-2").version == "v1"
    assert _bytes(root / "goals" / "GOAL-1.json") == goal1_before
    assert _bytes(root / "goals" / "GOAL-2.json") == goal2_before


def test_goal_revision_revise_plan_whole_family_path_sets_lineage(tmp_path):
    # The whole-family path (no subset) keeps its plan-wide revision
    # behavior and now marks every reopened goal with goal-level lineage.
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan
    frozen1 = _frozen_goal(root, "GOAL-1")

    revised = revise_plan(root, v1)

    assert revised.version == "v2-draft"
    for goal_id in ("GOAL-1", "GOAL-2"):
        goal = read_goal(root, goal_id)
        assert goal.frozen is False
        assert goal.version == "v2-draft"
        assert goal.parent_goal_id == goal_id
    # Content carries over from the frozen records (only the version and
    # freeze metadata are rewritten).
    assert read_goal(root, "GOAL-1").objective == frozen1.objective
    assert read_goal(root, "GOAL-1").title == frozen1.title


def test_goal_revision_revise_plan_subset_errors_write_nothing(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan
    amended = replace(_frozen_goal(root, "GOAL-1"), objective="Amended.")

    # An unregistered goal id in the subset.
    with pytest.raises(GoalNotFoundError) as exc:
        revise_plan(root, v1, goal_subset=(make_goal("GOAL-9"),))
    assert "GOAL-9" in str(exc.value)
    assert not (root / "plans" / "v2-draft.json").exists()

    # A stale goal object (version diverging from the frozen record).
    stale = replace(amended, version="v1-draft")
    with pytest.raises(GoalStateMismatchError):
        revise_plan(root, v1, goal_subset=(stale,))
    assert not (root / "plans" / "v2-draft.json").exists()

    # A draft goal (already reopened by an earlier revision).
    revise_goal(root, amended)
    with pytest.raises(GoalNotFrozenError):
        revise_plan(root, v1, goal_subset=(read_goal(root, "GOAL-1"),))
    assert not (root / "plans" / "v2-draft.json").exists()

    # The family is untouched by the rejected revisions: GOAL-1 keeps the
    # state revise_goal gave it, GOAL-2 stays frozen.
    assert read_goal(root, "GOAL-1").version == "v2-draft"
    assert _frozen_goal(root, "GOAL-2").version == "v1"


def test_goal_revision_revise_plan_subset_type_error_boundaries(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan
    with pytest.raises(TypeError):
        revise_plan(root, v1, goal_subset="GOAL-1")  # type: ignore
    with pytest.raises(TypeError):
        revise_plan(
            root, v1, goal_subset=[_frozen_goal(root, "GOAL-1")]  # type: ignore
        )
    with pytest.raises(TypeError):
        revise_plan(root, v1, goal_subset=(42,))  # type: ignore
    with pytest.raises(TypeError):
        revise_plan(
            root, v1, goal_subset=({"goal_id": "GOAL-1"},)  # type: ignore
        )


# ---------------------------------------------------------------------------
# Lineage is visible through the existing records
# ---------------------------------------------------------------------------


def test_goal_revision_lineage_goal_only_revision_leaves_plan_line(tmp_path):
    # A goal-only revision does not touch the plan line: no plan version
    # is created, the stored frozen plan stays FROZEN in the lineage.
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan

    revise_goal(root, replace(_frozen_goal(root, "GOAL-1"), objective="Amended."))

    entries = plan_lineage(root)
    assert [e.plan.version for e in entries] == ["v1-draft", "v1"]
    assert [e.status for e in entries] == [PlanStatus.DRAFT, PlanStatus.FROZEN]
    assert read_plan(root, "v1") == v1


def test_goal_revision_lineage_visible_via_goal_and_plan_records(tmp_path):
    root = build_two_goal_workspace(tmp_path)
    v1 = freeze_two_goals(root).frozen_plan

    revise_plan(
        root,
        v1,
        goal_subset=(replace(_frozen_goal(root, "GOAL-1"), objective="Amended."),),
    )

    # Goal-level lineage: the reopened goal names the frozen goal it was
    # revised from; the untouched sibling carries no lineage marker.
    assert read_goal(root, "GOAL-1").parent_goal_id == "GOAL-1"
    assert read_goal(root, "GOAL-1").version == "v2-draft"
    assert read_goal(root, "GOAL-2").parent_goal_id is None
    # Plan-level lineage: the next draft carries parent_plan_version and
    # the stored v1 record is reported SUPERSEDED (never mutated).
    assert read_plan(root, "v2-draft").parent_plan_version == "v1"
    entries = plan_lineage(root)
    assert [e.plan.version for e in entries] == ["v1-draft", "v1", "v2-draft"]
    assert [e.status for e in entries] == [
        PlanStatus.DRAFT,
        PlanStatus.SUPERSEDED,
        PlanStatus.DRAFT,
    ]
    assert read_plan(root, "v1").status is PlanStatus.FROZEN
