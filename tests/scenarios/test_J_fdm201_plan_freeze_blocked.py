"""FDM-201 simulated scenario J -- Plan completeness failure (DEV-M12-G06).

Scenario J is the frozen acceptance fixture of
``18-TEST-AND-ACCEPTANCE-PLAN.md``: **one SI result is intentionally
left unmapped** -- the C2H4 full adsorption isotherm ``INV-0304``
(EXISTENCE-ONLY, reported only in the non-downloaded SI) enters the
workspace inventory without any requirement mapping. Expected (frozen
acceptance, AC-05 of DEV-M12-G06): **Plan v1 freeze is PROHIBITED until
the inventory audit is repaired**; once the missing requirement mapping
is registered, the audit passes and the freeze is allowed.

Everything runs on the real machinery: the real inventory registry
(``planning.inventory`` ``register_inventory_item`` /
``register_requirement``, rule-computed mapping statuses), the real
completeness audit (``planning.audit`` -- ``R-AUD-U1`` fails the audit
on an unmapped formal item; stored ``mapping_status`` snapshots are
never trusted), the real goal-contract family registry
(``planning.plan`` ``register_goal`` / ``register_acceptance`` /
``register_analysis_protocol`` / ``register_closure_contract`` /
``build_plan_v1``) and the real plan freeze gate
(``planning.freeze`` ``freeze_plan`` --
``FreezeProhibitedError`` naming the offending item ids, nothing
written by a prohibited freeze; ``PlanAlreadyFrozenError`` on the
second freeze of ``v1``).

All grounding values are read live from the frozen ``benchmarks/fdm201/``
register files (``goals/mapping_audit.yaml`` -- 82/82 mapped in the
frozen benchmark -- ``goals/goals.yaml`` and ``inventory/INVENTORY.yaml``):
the scenario *removes* the mapping of ``INV-0304`` from the frozen state
to exercise the completeness gate, then *repairs* it by registering the
requirement the frozen audit resolves (``GOAL-EXE-50``).

Determinism mirrors the F/G/H suites: fixed identities/timestamps,
pinned safe ids, no randomness, no wall clock, no network.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    AuditStatus,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Confidence,
    Criticality,
    DecisionMode,
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    InventoryItemType,
    MappingStatus,
    PlanStatus,
    PrimaryOrExploratory,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
)
from scientific_reproduction.planning.audit import audit_inventory_registry
from scientific_reproduction.planning.freeze import (
    FreezeProhibitedError,
    PlanAlreadyFrozenError,
    freeze_plan,
)
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
)
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    build_plan_v1,
    read_plan,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
)

#: Deterministic author/committer identity (mirrors the other scenario suites).
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed freeze timestamp: the freeze is deterministic (as in the F/G/H suites).
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Primary target DOI of the one-paper project (17-FDM201-REFERENCE-CASE.md).
DOI = "10.1039/D5TA00771B"

#: The frozen benchmark register (read live, never copied into this file).
BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "fdm201"
MAPPING_AUDIT_YAML = BENCHMARK_ROOT / "goals" / "mapping_audit.yaml"
GOALS_YAML = BENCHMARK_ROOT / "goals" / "goals.yaml"
INVENTORY_YAML = BENCHMARK_ROOT / "inventory" / "INVENTORY.yaml"

#: The goal of scenario J: the frozen experimental isotherm goal both SI
#: results map to (GOAL-EXE-50, WP-50).
GOAL_ID = "GOAL-EXE-50"
GOAL_TITLE = "Single-component C3H6/C2H4 adsorption isotherms"

#: The two formally reported SI results: INV-0301 stays mapped, INV-0304
#: (the C2H4 full isotherm, EXISTENCE-ONLY) is intentionally left unmapped.
ITEM_MAPPED = "INV-0301"
ITEM_UNMAPPED = "INV-0304"
REQ_MAPPED = "REQ-J-0301"
REQ_REPAIR = "REQ-J-0304"

#: Goal-contract family ids of the scenario (all safe registry ids).
ACC_ID = "ACC-EXE-50"
ANL_ID = "ANL-EXE-50"
CLS_ID = "CLS-EXE-50"


# ---------------------------------------------------------------------------
# Deterministic project fixtures (self-contained: scenario tests live in
# their own directory, so the planning-suite helpers are not on the
# import path)
# ---------------------------------------------------------------------------


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def make_item_0301() -> ReproductionInventoryItem:
    """INV-0301: the reported C3H6 single-component uptake (mapped)."""
    return ReproductionInventoryItem(
        inventory_id=ITEM_MAPPED,
        source_id="SRC-TARGET-PAPER",
        item_type=InventoryItemType.EXPERIMENT,
        formal_report=True,
        description=(
            "Reported C3H6 single-component uptake at 298 K and 1 bar"
            " (180.5 cm3 g-1; value_status REPORTED-NON-FINAL,"
            " 17-FDM201-REFERENCE-CASE.md section 2 seed fact)"
        ),
        source_location="17-FDM201-REFERENCE-CASE.md section 2",
        mapping_status=MappingStatus.UNMAPPED,  # recomputed by registration
        requirement_ids=[REQ_MAPPED],
    )


def make_item_0304() -> ReproductionInventoryItem:
    """INV-0304: the C2H4 full adsorption isotherm -- the SI result whose
    requirement mapping scenario J intentionally leaves out of the
    registry (EXISTENCE-ONLY: the SI is not downloaded at registration).

    The item records its mapping intent (``REQ-J-0304``, mirroring the
    frozen benchmark's own ``requirement_mapping`` on INV-0304), but the
    requirement registration is deliberately omitted, so the registry
    holds no requirement mapping and the item reads AMBIGUOUS until the
    missing requirement is registered (R-MAP-A1 -- the mapping is
    missing, exactly the repair the scenario performs).
    """
    return ReproductionInventoryItem(
        inventory_id=ITEM_UNMAPPED,
        source_id="SRC-TARGET-PAPER",
        item_type=InventoryItemType.DATASET,
        formal_report=True,
        description=(
            "C2H4 full adsorption isotherm (point grid, pressure range);"
            " candidate WP-50 unit process, EXISTENCE-ONLY (reported only"
            " in the SI, not downloaded at registration)"
        ),
        source_location="17-FDM201-REFERENCE-CASE.md section 4 WP-50",
        mapping_status=MappingStatus.UNMAPPED,  # recomputed by registration
        requirement_ids=[REQ_REPAIR],  # mapping intent; requirement unregistered
    )


def make_requirement_0301() -> ReproductionRequirement:
    """The registered requirement mapping INV-0301 onto GOAL-EXE-50."""
    return ReproductionRequirement(
        requirement_id=REQ_MAPPED,
        statement=(
            "Reproduce the reported C3H6 single-component uptake at 298 K,"
            " 1 bar (INV-0301) against the frozen seed fact."
        ),
        inventory_items=[ITEM_MAPPED],
        criticality=Criticality.REQUIRED,
        goal_ids=[GOAL_ID],
        outcome=RequirementOutcome.OPEN,
    )


def make_requirement_0304() -> ReproductionRequirement:
    """The repair requirement mapping INV-0304 onto GOAL-EXE-50 (the
    mapping the frozen benchmark audit resolves; the audit passes only
    once this requirement is registered)."""
    return ReproductionRequirement(
        requirement_id=REQ_REPAIR,
        statement=(
            "Reproduce the reported C2H4 full adsorption isotherm (INV-0304,"
            " point grid and pressure range) within the GOAL-EXE-50 isotherm"
            " family."
        ),
        inventory_items=[ITEM_UNMAPPED],
        criticality=Criticality.REQUIRED,
        goal_ids=[GOAL_ID],
        outcome=RequirementOutcome.OPEN,
    )


def make_goal(*, requirement_ids: tuple[str, ...]) -> GoalContract:
    """The draft GOAL-EXE-50 contract (version ``v1-draft``)."""
    return GoalContract(
        goal_id=GOAL_ID,
        title=GOAL_TITLE,
        unit_process_type="gas_adsorption_isotherm",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective=(
            "Reproduce the formally reported single-component C3H6/C2H4"
            " adsorption isotherms."
        ),
        requirement_ids=list(requirement_ids),
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref=ACC_ID, frozen=False),
        analysis_protocol_ref=ANL_ID,
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref=CLS_ID,
    )


def make_acceptance() -> AcceptanceCriteria:
    """The draft acceptance record of GOAL-EXE-50 (v1-draft)."""
    return AcceptanceCriteria(
        acceptance_id=ACC_ID,
        goal_id=GOAL_ID,
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[
            {"metric": "uptake_at_defined_pressure", "rule": "equivalence_interval"}
        ],
        target={
            "metric": "uptake_at_defined_pressure",
            "published_seed_value_cm3_g": 180.5,
        },
        confidence=Confidence.LOW,
    )


def make_analysis() -> AnalysisProtocolOrResult:
    """The draft analysis protocol of GOAL-EXE-50 (v1-draft)."""
    return AnalysisProtocolOrResult(
        analysis_id=ANL_ID,
        kind=AnalysisKind.PROTOCOL,
        protocol_version=INITIAL_PLAN_VERSION,
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=False,
        methods=[{"name": "isotherm_fit"}],
    )


def make_closure() -> ClosureContract:
    """The draft closure contract of GOAL-EXE-50 (v1-draft)."""
    return ClosureContract(
        closure_id=CLS_ID,
        frozen=False,
        statistical_sufficiency={"min_valid_n": 3},
        execution_validity={"verified": True},
        diagnosis={"tolerances": {}},
        recovery=ClosureRecovery(),
        literature=ClosureLiterature(),
    )


@dataclass(frozen=True)
class ScenarioJResult:
    """The registered state of scenario J (frozen, auditable)."""

    root: Path
    repaired: bool


def execute_scenario_j(root: Path, *, repaired: bool = False) -> ScenarioJResult:
    """Execute scenario J end to end.

    Registers the two formally reported SI results (INV-0301 mapped,
    INV-0304 with its mapping intent recorded but its requirement
    deliberately unregistered -- the SI result's mapping is missing from
    the registry), the requirement mapping INV-0301 onto GOAL-EXE-50 and
    the draft goal-contract family. When ``repaired`` the missing
    requirement REQ-J-0304 mapping INV-0304 onto GOAL-EXE-50 is
    registered too -- the repair the frozen benchmark audit already
    resolves. Registration recomputes every mapping status by the real
    rule table; stored snapshots are never trusted by the audit.
    """
    root = init_project(root)
    register_inventory_item(root, make_item_0301())
    register_inventory_item(root, make_item_0304())
    register_requirement(root, make_requirement_0301())
    requirement_ids: tuple[str, ...]
    if repaired:
        register_requirement(root, make_requirement_0304())
        requirement_ids = (REQ_MAPPED, REQ_REPAIR)
    else:
        requirement_ids = (REQ_MAPPED,)
    register_goal(root, make_goal(requirement_ids=requirement_ids))
    register_acceptance(root, make_acceptance())
    register_analysis_protocol(root, make_analysis())
    register_closure_contract(root, make_closure())
    return ScenarioJResult(root=root, repaired=repaired)


# ---------------------------------------------------------------------------
# AC-05 (a): the scenario is grounded in the frozen FDM-201 mapping audit
# ---------------------------------------------------------------------------


def test_J_ac05_grounded_on_frozen_fdm201_mapping():
    # Scenario J is not an invented case: the frozen benchmark audit maps
    # both SI results -- INV-0301 and INV-0304 -- onto GOAL-EXE-50 with
    # 82/82 coverage and PASS, and the frozen goal carries the scenario's
    # title; the scenario *removes* the INV-0304 mapping, then *repairs*
    # it to the exact frozen resolution.
    with MAPPING_AUDIT_YAML.open(encoding="utf-8") as handle:
        audit = yaml.safe_load(handle)
    mapped = {m["item_id"]: m for m in audit["item_mappings"]}
    assert ITEM_MAPPED in mapped and ITEM_UNMAPPED in mapped
    assert GOAL_ID in mapped[ITEM_MAPPED]["mapped_to"]
    assert GOAL_ID in mapped[ITEM_UNMAPPED]["mapped_to"]
    assert audit["coverage"]["coverage"] == 1.0
    assert audit["coverage"]["status"] == "PASS"
    with GOALS_YAML.open(encoding="utf-8") as handle:
        goals = yaml.safe_load(handle)["goals"]
    goal = next(g for g in goals if g["goal_id"] == GOAL_ID)
    assert goal["title"] == GOAL_TITLE
    # INV-0304 is EXISTENCE-ONLY: its values live only in the non-downloaded
    # SI -- exactly the SI result the frozen benchmark still mapped.
    with INVENTORY_YAML.open(encoding="utf-8") as handle:
        inventory = yaml.safe_load(handle)
    inv_0304 = next(it for it in inventory["items"] if it["item_id"] == ITEM_UNMAPPED)
    assert inv_0304["value_status"] == "EXISTENCE-ONLY"
    assert inv_0304["title"] == "C2H4 full adsorption isotherm (point grid, pressure range)"


# ---------------------------------------------------------------------------
# AC-05 (b): the unmapped SI result fails the completeness audit
# ---------------------------------------------------------------------------


def test_J_ac05_unmapped_si_result_fails_the_inventory_audit(tmp_path):
    # The real completeness audit over the registered state: INV-0304's
    # requirement mapping is missing from the registry (REQ-J-0304 is not
    # registered), so the item reads AMBIGUOUS (R-MAP-A1) and the audit
    # fails R-AUD-A1 naming the offending item -- coverage 1 of 2 formal
    # items, freeze not eligible.
    scenario = execute_scenario_j(tmp_path)
    audit = audit_inventory_registry(scenario.root)
    assert audit.verdict is AuditStatus.FAIL
    assert audit.matched_rule_id == "R-AUD-A1"
    assert audit.ambiguous_item_ids == (ITEM_UNMAPPED,)
    assert audit.unmapped_item_ids == ()
    assert audit.offending_item_ids == (ITEM_UNMAPPED,)
    assert audit.freeze_eligible is False
    assert audit.summary.formally_reported_items == 2
    assert audit.summary.mapped_items == 1
    assert audit.summary.ambiguous_items == 1
    assert audit.summary.coverage == 0.5
    # Only INV-0304 offends; the mapped item is never flagged.
    assert ITEM_MAPPED not in audit.offending_item_ids


def test_J_ac05_fully_unmapped_item_blocks_freeze_without_registry_repair(tmp_path):
    # The audit vocabulary's other missing-mapping shape: an item
    # registered with NO requirement ids is UNMAPPED (R-MAP-U1) and the
    # freeze is prohibited; the inventory registry is immutable-functional
    # (items are written once), so registering a requirement that
    # *references* the item does not repair it -- the item itself must
    # record its mapping at registration (the shape execute_scenario_j
    # uses). Both shapes fail the freeze gate; only the recorded-mapping
    # shape is repairable by the registry.
    root = init_project(tmp_path)
    register_inventory_item(root, make_item_0301())
    register_inventory_item(
        root,
        ReproductionInventoryItem(
            inventory_id=ITEM_UNMAPPED,
            source_id="SRC-TARGET-PAPER",
            item_type=InventoryItemType.DATASET,
            formal_report=True,
            description=(
                "C2H4 full adsorption isotherm (point grid, pressure range);"
                " EXISTENCE-ONLY"
            ),
            source_location="17-FDM201-REFERENCE-CASE.md section 4 WP-50",
            mapping_status=MappingStatus.UNMAPPED,  # recomputed by registration
            requirement_ids=[],  # no mapping recorded at all
        ),
    )
    register_requirement(root, make_requirement_0301())
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.FAIL
    assert audit.matched_rule_id == "R-AUD-U1"
    assert audit.unmapped_item_ids == (ITEM_UNMAPPED,)
    assert audit.summary.coverage == 0.5
    # The referencing requirement cannot repair the item: the audit is a
    # pure function of the item's own recorded requirement ids.
    register_requirement(root, make_requirement_0304())
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.FAIL
    assert audit.unmapped_item_ids == (ITEM_UNMAPPED,)
    with pytest.raises(FreezeProhibitedError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert exc.value.offending_item_ids == (ITEM_UNMAPPED,)


def test_J_ac05_freeze_prohibited_until_audit_repaired(tmp_path):
    # AC-05: the Plan v1 freeze is PROHIBITED while INV-0304 is unmapped
    # (FreezeProhibitedError naming the offending item, nothing written),
    # and ALLOWED once the missing requirement mapping is registered.
    scenario = execute_scenario_j(tmp_path)
    with pytest.raises(FreezeProhibitedError) as exc:
        freeze_plan(scenario.root, build_plan_v1(scenario.root), timestamp=FROZEN_AT)
    assert exc.value.offending_item_ids == (ITEM_UNMAPPED,)
    assert ITEM_UNMAPPED in str(exc.value)
    # A prohibited freeze writes nothing.
    assert not (scenario.root / "plans" / "v1.json").exists()
    assert not (scenario.root / "plans" / "v1-draft.json").exists()
    # Repair: register the missing requirement mapping (the frozen audit's
    # resolution) -- the audit passes and the freeze is allowed.
    register_requirement(scenario.root, make_requirement_0304())
    audit = audit_inventory_registry(scenario.root)
    assert audit.verdict is AuditStatus.PASS
    assert audit.matched_rule_id == "R-AUD-P1"
    assert audit.freeze_eligible is True
    result = freeze_plan(scenario.root, build_plan_v1(scenario.root), timestamp=FROZEN_AT)
    assert result.frozen_plan.status is PlanStatus.FROZEN
    assert result.frozen_plan.version == "v1"
    assert (scenario.root / "plans" / "v1.json").is_file()


def test_J_ac05_stored_snapshot_never_trusted(tmp_path):
    # The audit recomputes every status from the registered requirement
    # set: the stored INV-0304 record still carries the AMBIGUOUS snapshot
    # (with the ambiguity note) from registration -- the registry never
    # rewrites item records -- yet after the repair the audit reads it
    # MAPPED and the freeze passes: stored snapshots are never trusted.
    scenario = execute_scenario_j(tmp_path, repaired=True)
    stored = json.loads(
        (scenario.root / "inventory" / f"{ITEM_UNMAPPED}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["mapping_status"] == "AMBIGUOUS"  # untouched snapshot
    assert REQ_REPAIR in stored["ambiguity_notes"]
    audit = audit_inventory_registry(scenario.root)
    assert audit.verdict is AuditStatus.PASS
    assert audit.ambiguous_item_ids == ()
    assert audit.summary.mapped_items == 2
    assert audit.summary.coverage == 1.0
    draft = build_plan_v1(scenario.root)
    assert draft.inventory_audit.status is AuditStatus.PASS
    assert draft.inventory_audit.mapped_items == 2
    freeze_plan(scenario.root, draft, timestamp=FROZEN_AT)  # allowed


# ---------------------------------------------------------------------------
# AC-05 (c): the repaired freeze produces the frozen Plan v1 contract
# ---------------------------------------------------------------------------


def test_J_ac05_frozen_plan_and_family_are_frozen(tmp_path):
    # After the repair the freeze yields the frozen v1 plan: FROZEN status,
    # the frozen timestamp, the PASS inventory audit embedded, both goal
    # mappings, and a fully frozen goal-contract family (never rewritten
    # drafts).
    scenario = execute_scenario_j(tmp_path, repaired=True)
    result = freeze_plan(scenario.root, build_plan_v1(scenario.root), timestamp=FROZEN_AT)
    frozen = result.frozen_plan
    assert frozen.status is PlanStatus.FROZEN
    assert frozen.version == "v1"
    assert frozen.frozen_at == "2026-06-01T00:00:00Z"
    assert frozen.goal_ids == [GOAL_ID]
    assert frozen.requirement_ids == [REQ_MAPPED, REQ_REPAIR]
    audit_view = frozen.inventory_audit
    assert audit_view.formally_reported_items == 2
    assert audit_view.mapped_items == 2
    assert audit_view.unmapped_items == 0
    assert audit_view.ambiguous_items == 0
    assert audit_view.coverage == 1.0
    assert audit_view.status is AuditStatus.PASS
    assert [g.goal_id for g in result.goals] == [GOAL_ID]
    assert all(g.frozen for g in result.goals)
    assert result.acceptance[0].acceptance_id == ACC_ID
    assert all(a.frozen for a in result.acceptance)
    assert result.analysis_protocols[0].analysis_id == ANL_ID
    assert all(a.frozen for a in result.analysis_protocols)
    assert result.closure_contracts[0].closure_id == CLS_ID
    assert all(c.frozen for c in result.closure_contracts)
    assert result.frozen_plan == read_plan(scenario.root, "v1")


def test_J_ac05_second_freeze_rejected_immutable(tmp_path):
    # Plan records are immutable-functional: a second freeze of v1 is
    # rejected (PlanAlreadyFrozenError) and the stored record is
    # byte-identical -- the repair cannot be replayed over the frozen plan.
    scenario = execute_scenario_j(tmp_path, repaired=True)
    freeze_plan(scenario.root, build_plan_v1(scenario.root), timestamp=FROZEN_AT)
    stored_path = scenario.root / "plans" / "v1.json"
    before = stored_path.read_bytes()
    with pytest.raises(PlanAlreadyFrozenError):
        freeze_plan(scenario.root, build_plan_v1(scenario.root), timestamp=FROZEN_AT)
    assert stored_path.read_bytes() == before
    assert len(list((scenario.root / "plans").glob("v1*.json"))) == 2  # draft + v1


# ---------------------------------------------------------------------------
# Paradigm boundaries (determinism, safe ids, frozen records)
# ---------------------------------------------------------------------------


def test_J_ac05_deterministic_across_workspaces(tmp_path):
    # Same state -> same freeze: the audit records and the stored frozen
    # plan bytes are identical across fresh workspaces.
    first = execute_scenario_j(tmp_path / "first", repaired=True)
    second = execute_scenario_j(tmp_path / "second", repaired=True)
    assert audit_inventory_registry(first.root) == audit_inventory_registry(
        second.root
    )
    first_result = freeze_plan(
        first.root, build_plan_v1(first.root), timestamp=FROZEN_AT
    )
    second_result = freeze_plan(
        second.root, build_plan_v1(second.root), timestamp=FROZEN_AT
    )
    assert first_result.frozen_plan == second_result.frozen_plan
    assert (
        (first.root / "plans" / "v1.json").read_bytes()
        == (second.root / "plans" / "v1.json").read_bytes()
    )
    assert all(
        g1 == g2 for g1, g2 in zip(first_result.goals, second_result.goals)
    )


def test_J_ac05_scenario_records_reject_mutation(tmp_path):
    # Frozen dataclasses throughout the scenario: the registered items,
    # requirements, the goal family drafts and the freeze result cannot
    # be mutated after construction.
    scenario = execute_scenario_j(tmp_path, repaired=True)
    result = freeze_plan(scenario.root, build_plan_v1(scenario.root), timestamp=FROZEN_AT)
    records = (
        make_item_0301(),
        make_item_0304(),
        make_requirement_0301(),
        make_requirement_0304(),
        make_goal(requirement_ids=(REQ_MAPPED, REQ_REPAIR)),
        result.frozen_plan,
        result,
    )
    for record in records:
        assert is_dataclass(record)
        field_name = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, None)


def test_J_ac05_scenario_uses_safe_ids_only(tmp_path):
    # Every id on the scenario path is a safe registry id.
    scenario = execute_scenario_j(tmp_path, repaired=True)
    ids = (
        GOAL_ID,
        ITEM_MAPPED,
        ITEM_UNMAPPED,
        REQ_MAPPED,
        REQ_REPAIR,
        ACC_ID,
        ANL_ID,
        CLS_ID,
    )
    for value in ids:
        assert value not in ("", ".", "..")
        assert "/" not in value and "\\" not in value
        assert not any(char in value for char in "*?[]")
    freeze_plan(scenario.root, build_plan_v1(scenario.root), timestamp=FROZEN_AT)
    stored = {p.name for p in (scenario.root / "inventory").glob("*.json")}
    assert stored == {f"{ITEM_MAPPED}.json", f"{ITEM_UNMAPPED}.json"}
    stored = {p.name for p in (scenario.root / "requirements").glob("*.json")}
    assert stored == {f"{REQ_MAPPED}.json", f"{REQ_REPAIR}.json"}
