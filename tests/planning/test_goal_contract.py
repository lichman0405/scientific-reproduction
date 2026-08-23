"""Typed Goal Contract procedure and execution constraints (issue #156).

The frozen Goal Contract must carry a machine-verifiable operational
design: the typed ``procedure`` (ordered steps with action/inputs/outputs/
trace refs, aligned with the LabExecutionPackage step vocabulary) and the
typed ``execution_constraints`` (structured 05-GOAL-RUN-SCHEMA.md SS4
semantics: hardware/environment constraints, forbidden changes, safety
notes). Acceptance:

* the schema gate requires both keys: a goal record without them fails
  ``validate_and_reject``, with them it passes;
* the freeze writes both fields on every frozen record;
* existing registered goals -- records written before the fields existed
  -- load through the documented accept-and-migrate path: absence reads
  as an explicitly empty procedure / empty constraints (model defaults),
  and the freeze re-persists the record carrying both keys. (The FDM-201
  benchmark register predates the fields and reloads through this path;
  ``tests/benchmarks/test_fdm201_reload_audit.py`` is the migration
  acceptance that the reload passes unchanged.)

The deterministic path mirrors ``inventory_helpers`` (fixed identities and
timestamps); the workspace builders mirror ``test_freeze.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from inventory_helpers import init_project, make_item, make_requirement

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Confidence,
    DecisionMode,
    GoalAcceptance,
    GoalContract,
    GoalExecutionConstraints,
    GoalProcedureStep,
    GoalReplication,
    GoalTrack,
    PrimaryOrExploratory,
)
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    validate_and_reject,
    validate_object,
)
from scientific_reproduction.planning.freeze import freeze_plan
from scientific_reproduction.planning.init import INITIAL_PLAN_VERSION
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    build_plan_v1,
    list_goals,
    read_goal,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
)

#: Fixed freeze timestamp: every freeze in this suite is deterministic.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)


def make_goal(
    goal_id: str,
    *,
    procedure: list[GoalProcedureStep] | None = None,
    execution_constraints: GoalExecutionConstraints | None = None,
) -> GoalContract:
    """Build a schema-valid draft goal contract (version ``v1-draft``).

    The typed contract fields default to a realistic procedure /
    constraint set; the migration tests pass ``[]`` / the empty object
    explicitly.
    """
    return GoalContract(
        goal_id=goal_id,
        title=f"Reproduce the reported isotherm ({goal_id}).",
        unit_process_type="gas_adsorption_isotherm",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="Reproduce the formally reported isotherm dataset.",
        requirement_ids=["REQ-1", "REQ-2"],
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref="ACC-1", frozen=False),
        analysis_protocol_ref="ANL-1",
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref="CLS-1",
        procedure=(
            [
                GoalProcedureStep(
                    action="acquire isotherm",
                    inputs=["sample", "gas: C3H6"],
                    outputs=["raw_isotherm_data"],
                    trace_refs=["REQ-1"],
                )
            ]
            if procedure is None
            else procedure
        ),
        execution_constraints=(
            GoalExecutionConstraints(
                environment={"temperature_K": 298},
                forbidden_changes=["reported measurement temperature"],
                safety_notes=["pressurized gas handling per lab SOP"],
            )
            if execution_constraints is None
            else execution_constraints
        ),
    )


def make_acceptance() -> AcceptanceCriteria:
    """Build a schema-valid draft acceptance record (version ``v1-draft``)."""
    return AcceptanceCriteria(
        acceptance_id="ACC-1",
        goal_id="GOAL-1",
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[
            {"metric": "batch_level_uptake", "rule": "equivalence_interval"}
        ],
        target={"metric": "uptake_at_defined_pressure"},
        confidence=Confidence.LOW,
    )


def make_analysis() -> AnalysisProtocolOrResult:
    """Build a schema-valid draft analysis protocol (version ``v1-draft``)."""
    return AnalysisProtocolOrResult(
        analysis_id="ANL-1",
        kind=AnalysisKind.PROTOCOL,
        protocol_version=INITIAL_PLAN_VERSION,
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=False,
        methods=[{"name": "isotherm_fit"}],
    )


def make_closure() -> ClosureContract:
    """Build a schema-valid draft closure contract."""
    return ClosureContract(
        closure_id="CLS-1",
        frozen=False,
        statistical_sufficiency={"min_valid_n": 3},
        execution_validity={"verified": True},
        diagnosis={"tolerances": {}},
        recovery=ClosureRecovery(),
        literature=ClosureLiterature(),
    )


def build_complete_workspace(root: Path) -> Path:
    """Initialize a freeze-eligible workspace with the full goal family."""
    init_project(root)
    register_inventory_item(
        root, make_item("ITEM-1", requirement_ids=("REQ-1",))
    )
    register_inventory_item(
        root, make_item("ITEM-2", requirement_ids=("REQ-2",))
    )
    register_requirement(
        root,
        make_requirement("REQ-1", inventory_items=("ITEM-1",), goal_ids=("GOAL-1",)),
    )
    register_requirement(
        root,
        make_requirement("REQ-2", inventory_items=("ITEM-2",), goal_ids=("GOAL-1",)),
    )
    register_goal(root, make_goal("GOAL-1"))
    register_acceptance(root, make_acceptance())
    register_analysis_protocol(root, make_analysis())
    register_closure_contract(root, make_closure())
    return root


def _canonical(data: dict) -> str:
    """The registry's canonical JSON serialization."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def _write_legacy_goal_record(root: Path, goal_id: str) -> None:
    """Rewrite a registered goal record as a pre-issue-#156 legacy record.

    The record predates the typed contract fields: ``procedure`` and
    ``execution_constraints`` are removed from the stored JSON (a
    simulation of a record written by a runtime before the fields
    existed -- readers never rewrite stored records, so no current API
    can produce one).
    """
    path = root / "goals" / f"{goal_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "procedure" in data and "execution_constraints" in data
    del data["procedure"]
    del data["execution_constraints"]
    atomic_write(path, _canonical(data))


# ---------------------------------------------------------------------------
# The schema gate requires the typed contract fields
# ---------------------------------------------------------------------------


def test_goal_record_without_contract_fields_fails_the_schema_gate():
    # AC: a goal record without procedure / execution_constraints fails
    # schema validation, naming both keys; a record with them passes.
    doc = make_goal("GOAL-1").to_dict()
    del doc["procedure"]
    del doc["execution_constraints"]
    errors = validate_object("goal", doc)
    assert errors
    assert any("procedure" in error for error in errors)
    assert any("execution_constraints" in error for error in errors)
    with pytest.raises(SchemaValidationError):
        validate_and_reject("goal", doc)


def test_goal_record_with_contract_fields_passes_the_schema_gate():
    # AC: a record carrying both fields (typed content) validates cleanly,
    # including the typed step/constraint shapes.
    doc = make_goal("GOAL-1").to_dict()
    assert doc["procedure"][0]["action"] == "acquire isotherm"
    assert doc["execution_constraints"]["forbidden_changes"] == [
        "reported measurement temperature"
    ]
    assert validate_object("goal", doc) == []
    assert validate_and_reject("goal", doc) is None


def test_goal_schema_typing_rejects_malformed_steps():
    # The typed shapes are enforced: a step without an action, a
    # non-string step input, non-string constraint entries, and a
    # non-object constraints value all fail the gate.
    doc = make_goal("GOAL-1").to_dict()
    doc["procedure"] = [{"inputs": ["x"]}]
    assert any("action" in error for error in validate_object("goal", doc))

    doc = make_goal("GOAL-1").to_dict()
    doc["procedure"] = [{"action": "a", "inputs": [1]}]
    assert any("inputs" in error for error in validate_object("goal", doc))

    doc = make_goal("GOAL-1").to_dict()
    doc["execution_constraints"] = {"forbidden_changes": [1]}
    assert any("forbidden_changes" in error for error in validate_object("goal", doc))

    doc = make_goal("GOAL-1").to_dict()
    doc["execution_constraints"] = "not-an-object"
    assert any("execution_constraints" in error for error in validate_object("goal", doc))


def test_goal_contract_exposes_typed_not_freeform_fields():
    # AC: GoalContract exposes typed fields -- procedure steps coerce to
    # GoalProcedureStep and constraints to GoalExecutionConstraints; a
    # plain model default is the documented empty migration state, and
    # to_dict always emits both keys (the schema requires them).
    goal = make_goal("GOAL-1")
    assert isinstance(goal.procedure, list)
    assert all(isinstance(step, GoalProcedureStep) for step in goal.procedure)
    assert isinstance(goal.execution_constraints, GoalExecutionConstraints)
    bare = GoalContract.from_dict(
        {k: v for k, v in goal.to_dict().items() if k not in ("procedure", "execution_constraints")}
    )
    assert bare.procedure == []
    assert bare.execution_constraints == GoalExecutionConstraints()
    assert "procedure" in bare.to_dict()
    assert "execution_constraints" in bare.to_dict()


# ---------------------------------------------------------------------------
# The freeze writes the typed contract fields
# ---------------------------------------------------------------------------


def test_freeze_writes_the_typed_contract_fields(tmp_path):
    # AC: the freeze persists both fields on every frozen goal record;
    # the stored record, the registry read and the freeze result agree,
    # with typed (never free-form) values.
    root = build_complete_workspace(tmp_path)
    result = freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)

    stored = json.loads((root / "goals" / "GOAL-1.json").read_text(encoding="utf-8"))
    assert stored["procedure"][0]["action"] == "acquire isotherm"
    assert stored["procedure"][0]["trace_refs"] == ["REQ-1"]
    assert stored["execution_constraints"]["environment"] == {"temperature_K": 298}
    assert stored["execution_constraints"]["forbidden_changes"] == [
        "reported measurement temperature"
    ]
    assert stored["execution_constraints"]["safety_notes"] == [
        "pressurized gas handling per lab SOP"
    ]
    assert stored["frozen"] is True

    frozen = result.goals[0]
    assert frozen.procedure == [GoalProcedureStep(
        action="acquire isotherm",
        inputs=["sample", "gas: C3H6"],
        outputs=["raw_isotherm_data"],
        trace_refs=["REQ-1"],
    )]
    assert frozen.execution_constraints == GoalExecutionConstraints(
        environment={"temperature_K": 298},
        forbidden_changes=["reported measurement temperature"],
        safety_notes=["pressurized gas handling per lab SOP"],
    )
    assert read_goal(root, "GOAL-1") == frozen


# ---------------------------------------------------------------------------
# Accept-and-migrate: existing records written before the fields existed
# ---------------------------------------------------------------------------


def test_existing_registered_goal_loads_via_the_migration_path(tmp_path):
    # AC: a goal record written before the typed fields existed loads
    # through the documented accept-and-migrate path -- absence reads as
    # an explicitly empty procedure / empty constraints -- and the read
    # never rewrites the stored file.
    root = build_complete_workspace(tmp_path)
    path = root / "goals" / "GOAL-1.json"
    _write_legacy_goal_record(root, "GOAL-1")
    legacy_bytes = path.read_bytes()

    loaded = read_goal(root, "GOAL-1")
    assert loaded.procedure == []
    assert loaded.execution_constraints == GoalExecutionConstraints()
    assert list_goals(root) == (loaded,)
    # Readers never rewrite stored records (AC-03): the legacy file is
    # untouched by the read.
    assert path.read_bytes() == legacy_bytes


def test_freeze_migrates_a_legacy_goal_record_in_place(tmp_path):
    # AC: the freeze re-persists a migrated legacy record carrying both
    # schema-required keys (the empty migration defaults, explicitly --
    # never silently frozen as authored content), and the migrated record
    # round-trips through the schema gate.
    root = build_complete_workspace(tmp_path)
    _write_legacy_goal_record(root, "GOAL-1")

    result = freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    stored = json.loads((root / "goals" / "GOAL-1.json").read_text(encoding="utf-8"))
    assert stored["procedure"] == []
    assert stored["execution_constraints"] == {
        "environment": {},
        "forbidden_changes": [],
        "safety_notes": [],
    }
    assert result.goals[0].procedure == []
    assert validate_object("goal", stored) == []


def test_register_goal_mapping_without_contract_fields_persists_migration_defaults(tmp_path):
    # The accept-and-migrate path also covers authoring: a mapping without
    # the fields registers with the documented empty defaults persisted
    # (the schema gate sees a record carrying both keys).
    root = build_complete_workspace(tmp_path)
    data = make_goal("GOAL-2").to_dict()
    del data["procedure"]
    del data["execution_constraints"]
    registered = register_goal(root, data)
    assert registered.procedure == []
    assert registered.execution_constraints == GoalExecutionConstraints()
    stored = json.loads((root / "goals" / "GOAL-2.json").read_text(encoding="utf-8"))
    assert stored["procedure"] == []
    assert "execution_constraints" in stored
