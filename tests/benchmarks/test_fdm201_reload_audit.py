"""FDM-201 benchmark-state reload and audit -- AC-03 (DEV-M12-G07).

AC-03 of the frozen DEV-M12-G07 goal (see
``benchmarks/fdm201/reviews/REVIEW-FDM201-RELOAD-001.yaml``): **benchmark
state can be reloaded and audited**. This file proves the frozen FDM-201
benchmark register under ``benchmarks/fdm201/`` is a loadable, auditable
state:

1. the frozen project record reloads into a fresh workspace through the
   real ``initialize_project`` API (fixed identity and the frozen
   ``2026-08-14T00:00:00Z`` timestamp), and the full 82-item /
   82-requirement inventory reloads through the real registry APIs
   (``register_inventory_item`` / ``register_requirement``, items first);
2. the real completeness audit over the reloaded state re-derives the
   frozen verdict: **PASS (R-AUD-P1), 82/82 mapped, coverage 1.0**, the
   exact counts of the frozen ``mapping_audit.yaml`` / ``plan_v1.yaml``
   ``inventory_audit`` blocks, exposed through the frozen
   ``PlanInventoryAudit`` view (``plan_inventory_audit()``);
3. the reload is deterministic: two fresh workspaces replay
   byte-identical state trees (the determinism convention of the
   scenario suites);
4. the **full goal-contract family reloads** -- all 20 frozen goals with
   their acceptance criteria, the 10 analysis protocols (ANL-001..090)
   and the 4 closure contracts (CC-*), every value read from the frozen
   register -- and ``freeze_plan`` round-trips the reloaded state to a
   frozen Plan v1 whose embedded inventory audit is the frozen 82/82
   PASS, at the frozen timestamp, with the whole family frozen by the
   freeze (the registry's contract-frozen-by-freeze semantics);
5. frozen register facts the current registry API cannot express are
   asserted directly against the frozen files, with the API gap
   documented (no source change):

   - ``goals.yaml`` goals carry **inline acceptance-criteria text**
     (e.g. GOAL-EXE-50 ``acceptance_criteria`` AC-01..AC-04), while the
     registry ``GoalContract.acceptance`` model requires an acceptance
     record ``criteria_ref`` id -- the G05 finding from PR #75; the
     reload derives a criteria_ref (``ACC-<GOAL_ID>``) and preserves the
     frozen AC text in the acceptance record's criteria list, and the
     frozen inline text is asserted directly;
   - there is **no ``register_assumption`` API** in the registry (the
     assumptions register is a benchmark planning artifact), so the
     46-entry assumptions register is asserted via the frozen file and
     core-schema validation (see the planning-benchmark file).

Determinism: fixed identities, fixed timestamps (project init
``2026-01-01``, freeze ``2026-08-14T00:00:00Z``), no wall clock, no
randomness, no network. The system under test is the real planning
machinery -- never mocked.
"""

from __future__ import annotations

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
    DependencyType,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
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

#: The frozen FDM-201 benchmark register (read live, never copied).
BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "fdm201"
REPO_ROOT = Path(__file__).resolve().parents[2]
MAPPING_AUDIT_YAML = BENCHMARK_ROOT / "goals" / "mapping_audit.yaml"
GOALS_YAML = BENCHMARK_ROOT / "goals" / "goals.yaml"
INVENTORY_YAML = BENCHMARK_ROOT / "inventory" / "INVENTORY.yaml"
PLAN_V1_YAML = BENCHMARK_ROOT / "plans" / "plan_v1.yaml"
ANALYSIS_PLAN_YAML = BENCHMARK_ROOT / "plans" / "analysis_plan.yaml"
CLOSURE_YAML = BENCHMARK_ROOT / "plans" / "closure.yaml"
PROJECT_YAML = BENCHMARK_ROOT / "project" / "project.yaml"

#: Deterministic author/committer identity (mirrors the scenario suites).
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed freeze timestamp: the frozen benchmark timestamp itself, so the
#: reloaded freeze reproduces the frozen ``frozen_at`` value.
FROZEN_AT = datetime(2026, 8, 14, tzinfo=timezone.utc)

#: Primary target DOI of the one-paper project (17-FDM201-REFERENCE-CASE.md).
DOI = "10.1039/D5TA00771B"

#: The frozen project record identity (project/project.yaml).
FROZEN_PROJECT_ID = "sr_project_c0e2b9a52c011ab5acd33a4feb732b4d"

#: The frozen benchmark timestamp (every frozen register file).
FROZEN_TIMESTAMP = "2026-08-14T00:00:00Z"

#: The representative goal whose family values are spot-asserted.
GOAL_ID = "GOAL-EXE-50"

#: Category -> registry item type for the reload. The frozen
#: INVENTORY.yaml has no such table (its record kind is a benchmark
#: inventory, not a registry inventory-item), so the mapping is defined
#: here per category semantics: 00 publication/source identity -> OTHER;
#: a structure/identity -> CHARACTERIZATION; d derived analysis ->
#: ANALYSIS; f simulation/computation -> COMPUTATION; b porosity, c
#: isotherms, e breakthrough and g synthesis/activation -> EXPERIMENT.
#: (Same table as the planning-benchmark acceptance file.)
CATEGORY_ITEM_TYPE = {
    "00": InventoryItemType.OTHER,
    "a": InventoryItemType.CHARACTERIZATION,
    "b": InventoryItemType.EXPERIMENT,
    "c": InventoryItemType.EXPERIMENT,
    "d": InventoryItemType.ANALYSIS,
    "e": InventoryItemType.EXPERIMENT,
    "f": InventoryItemType.COMPUTATION,
    "g": InventoryItemType.EXPERIMENT,
}


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _frozen_inventory() -> dict:
    return _load_yaml(INVENTORY_YAML)


def _frozen_goals() -> dict:
    return _load_yaml(GOALS_YAML)


def _wp_alias() -> dict[str, str]:
    return dict(_frozen_goals()["wp_alias"])


def resolve_goal_refs(refs: list[str]) -> tuple[str, ...]:
    """Resolve frozen mapping refs (GOAL-* / WP-*) onto defined goal ids
    via the frozen wp_alias table; deterministic sorted order."""
    alias = _wp_alias()
    defined = {g["goal_id"] for g in _frozen_goals()["goals"]}
    resolved = {alias.get(ref, ref) for ref in refs}
    assert all(g in defined for g in resolved)
    return tuple(sorted(resolved))


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root`` with the
    frozen project record identity; return ``root``."""
    initialize_project(
        root,
        DOI,
        project_id=FROZEN_PROJECT_ID,
        timestamp=TIMESTAMP,
        identity=IDENTITY,
    )
    return root


def make_item(frozen: dict) -> ReproductionInventoryItem:
    """Rebuild one frozen inventory item as a registry inventory item."""
    source_ids = (frozen.get("provenance") or {}).get("source_ids") or []
    return ReproductionInventoryItem(
        inventory_id=frozen["item_id"],
        source_id=source_ids[0] if source_ids else "SRC-TARGET-PAPER",
        item_type=CATEGORY_ITEM_TYPE[frozen["category"]],
        formal_report=True,
        description=(
            f"{frozen['title']} (reloaded from benchmarks/fdm201/inventory/"
            f"INVENTORY.yaml, category {frozen['category']})"
        ),
        source_location="benchmarks/fdm201/inventory/INVENTORY.yaml",
        mapping_status=MappingStatus.UNMAPPED,  # recomputed by registration
        requirement_ids=[frozen["item_id"]],
    )


def make_requirement(frozen: dict) -> ReproductionRequirement:
    """Rebuild the requirement mapping one frozen item onto its resolved
    frozen goals (requirement_id == item_id, mirroring the frozen plan's
    ``requirement_ids`` which are the INV-* ids)."""
    return ReproductionRequirement(
        requirement_id=frozen["item_id"],
        statement=(
            f"Reproduce the formally reported item {frozen['item_id']}:"
            f" {frozen['title']}."
        ),
        inventory_items=[frozen["item_id"]],
        criticality=Criticality.REQUIRED,
        goal_ids=list(resolve_goal_refs(frozen["requirement_mapping"])),
        outcome=RequirementOutcome.OPEN,
    )


def reload_full_inventory(root: Path) -> None:
    """Reload the full frozen 82-item / 82-requirement inventory into the
    workspace through the real registry APIs (items first, per the
    registry contract)."""
    for frozen in _frozen_inventory()["items"]:
        register_inventory_item(root, make_item(frozen))
    for frozen in _frozen_inventory()["items"]:
        register_requirement(root, make_requirement(frozen))


def make_goal(frozen: dict) -> GoalContract:
    """Rebuild one frozen goal contract through the registry GoalContract
    shape. The frozen inline acceptance text cannot be expressed by the
    goal record (its acceptance model is a criteria_ref id -- the
    documented G05/PR#75 API gap); the reload preserves the text in the
    acceptance record instead (see ``make_acceptance``)."""
    return GoalContract(
        goal_id=frozen["goal_id"],
        title=frozen["title"],
        unit_process_type=frozen["unit_process_type"],
        track=GoalTrack.STRICT_REPRODUCTION,
        objective=frozen["objective"],
        requirement_ids=list(frozen["requirement_ids"]),
        dependencies=[
            GoalDependency(
                goal_id=dep["goal_id"],
                type=DependencyType(dep["type"]),
                execution_gate=bool(dep.get("execution_gate", False)),
                acceptance_gate=bool(dep.get("acceptance_gate", False)),
            )
            for dep in frozen.get("dependencies", [])
        ],
        acceptance=GoalAcceptance(
            criteria_ref=f"ACC-{frozen['goal_id']}", frozen=False
        ),
        analysis_protocol_ref=frozen["analysis_protocol_ref"],
        replication=GoalReplication(
            independent_required=frozen["replication"]["independent_required"],
            planned_n_policy=frozen["replication"]["planned_n_policy"],
            minimum_n=frozen["replication"].get("minimum_n"),
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref=frozen["closure_contract_ref"],
        resource_ids=list(frozen.get("resource_ids", [])),
        assumption_ids=list(frozen.get("assumption_ids", [])),
    )


def make_acceptance(frozen: dict) -> AcceptanceCriteria:
    """The reloaded acceptance record of one frozen goal: the frozen
    inline AC text is preserved verbatim as the criteria metrics (the
    registry vocabulary closest to the frozen inline acceptance)."""
    return AcceptanceCriteria(
        acceptance_id=f"ACC-{frozen['goal_id']}",
        goal_id=frozen["goal_id"],
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[
            {"metric": text, "rule": "frozen_benchmark_acceptance"}
            for text in frozen["acceptance_criteria"]
        ],
        target={"metric": "frozen_benchmark_acceptance", "source": "goals.yaml"},
        confidence=Confidence.LOW,
    )


def make_analysis(frozen: dict) -> AnalysisProtocolOrResult:
    """The reloaded analysis protocol, values read from the frozen
    analysis plan register (registered once per protocol id)."""
    return AnalysisProtocolOrResult(
        analysis_id=frozen["analysis_id"],
        kind=AnalysisKind(frozen["kind"]),
        protocol_version=frozen["protocol_version"],
        primary_or_exploratory=PrimaryOrExploratory(
            frozen["primary_or_exploratory"]
        ),
        profile=AnalysisProfile(frozen["profile"]),
        frozen=False,
        methods=[{"step": m} for m in frozen["methods"]],
    )


def make_closure(frozen: dict) -> ClosureContract:
    """The reloaded closure contract, values read from the frozen closure
    register (registered once per contract id)."""
    recovery = frozen.get("recovery") or {}
    literature = frozen.get("literature") or {}
    return ClosureContract(
        closure_id=frozen["closure_id"],
        frozen=False,
        statistical_sufficiency=frozen.get("statistical_sufficiency", {}),
        execution_validity=frozen.get("execution_validity", {}),
        diagnosis=frozen.get("diagnosis", {}),
        recovery=ClosureRecovery(
            eligibility_rule=recovery.get("eligibility_rule", {}),
            eligible_hypotheses_total=recovery.get("eligible_hypotheses_total"),
            tested_or_ruled_out=recovery.get("tested_or_ruled_out"),
            remaining=recovery.get("remaining"),
        ),
        literature=ClosureLiterature(
            required_search_families_completed=literature.get(
                "required_search_families_completed"
            ),
            consecutive_zero_novelty_cycles=literature.get(
                "consecutive_zero_novelty_cycles"
            ),
            required_zero_novelty_cycles=literature.get(
                "required_zero_novelty_cycles", 2
            ),
        ),
    )


def reload_goal_families(root: Path) -> None:
    """Reload the full frozen goal-contract family: all 20 goals, their
    20 acceptance records, the 10 analysis protocols (ANL-001..090, once
    each) and the 4 closure contracts (CC-*, once each), every value read
    from the frozen register."""
    frozen_goals = _frozen_goals()["goals"]
    for frozen in frozen_goals:
        register_goal(root, make_goal(frozen))
    for frozen in frozen_goals:
        register_acceptance(root, make_acceptance(frozen))
    for protocol in _load_yaml(ANALYSIS_PLAN_YAML)["protocols"]:
        register_analysis_protocol(root, make_analysis(protocol))
    for contract in _load_yaml(CLOSURE_YAML)["closure_contracts"]:
        register_closure_contract(root, make_closure(contract))


def execute_reload(root: Path) -> Path:
    """Reload the full frozen state (project + inventory + goal family)
    into a fresh workspace; return ``root``."""
    init_project(root)
    reload_full_inventory(root)
    reload_goal_families(root)
    return root


def tree_bytes(root: Path) -> bytes:
    """Byte-identical snapshot of the durable state tree with the
    workspace's own absolute path normalized out (mirrors the scenario
    suites). The git working tree created by ``initialize_project`` is
    internal repository metadata, not benchmark state."""
    raw_root = str(root).encode("utf-8")
    escaped_root = raw_root.replace(b"\\", b"\\\\")
    chunks: list[bytes] = []
    for p in sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(root).as_posix(),
    ):
        if ".git" in p.relative_to(root).parts:
            continue
        chunks.append(
            p.read_bytes()
            .replace(escaped_root, b"<workspace>")
            .replace(raw_root, b"<workspace>")
        )
    return b"\n".join(chunks)


# ---------------------------------------------------------------------------
# AC-03 (a): the full frozen inventory reloads and re-audits to PASS
# ---------------------------------------------------------------------------


def test_fdm201_reload_full_inventory_audit_matches_frozen(tmp_path):
    # AC-03: the full 82-item / 82-requirement frozen inventory reloads
    # through the real registry APIs and the real completeness audit
    # re-derives the frozen verdict -- PASS (R-AUD-P1), 82/82 mapped,
    # coverage 1.0 -- with zero offending items.
    root = init_project(tmp_path)
    reload_full_inventory(root)
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.PASS
    assert audit.matched_rule_id == "R-AUD-P1"
    assert audit.freeze_eligible is True
    assert audit.summary.formally_reported_items == 82
    assert audit.summary.mapped_items == 82
    assert audit.summary.unmapped_items == 0
    assert audit.summary.ambiguous_items == 0
    assert audit.summary.coverage == 1.0
    assert audit.unmapped_item_ids == ()
    assert audit.ambiguous_item_ids == ()
    assert audit.offending_item_ids == ()


def test_fdm201_reload_audit_view_equals_frozen_audit_counts(tmp_path):
    # The frozen PlanInventoryAudit view of the re-derived audit equals
    # the frozen inventory_audit block of mapping_audit.yaml and
    # plan_v1.yaml exactly (82/82/0/0/1.0/PASS).
    root = init_project(tmp_path)
    reload_full_inventory(root)
    audit = audit_inventory_registry(root)
    view = audit.plan_inventory_audit()
    frozen_audit = _load_yaml(MAPPING_AUDIT_YAML)["coverage"]
    frozen_plan = _load_yaml(PLAN_V1_YAML)["inventory_audit"]
    assert view.formally_reported_items == frozen_audit["formally_reported_items"]
    assert view.formally_reported_items == frozen_plan["formally_reported_items"]
    assert view.mapped_items == frozen_audit["mapped_items"]
    assert view.mapped_items == frozen_plan["mapped_items"]
    assert view.unmapped_items == frozen_audit["unmapped_items"] == 0
    assert view.unmapped_items == frozen_plan["unmapped_items"]
    assert view.ambiguous_items == frozen_audit["ambiguous_items"] == 0
    assert view.ambiguous_items == frozen_plan["ambiguous_items"]
    assert view.coverage == frozen_audit["coverage"] == 1.0
    assert view.coverage == frozen_plan["coverage"]
    assert view.status is AuditStatus.PASS
    assert view.status.value == frozen_audit["status"] == frozen_plan["status"]


def test_fdm201_reload_covers_the_frozen_goal_and_requirement_ids(tmp_path):
    # The reloaded requirement set is exactly the frozen plan's
    # requirement_ids (the INV-* ids), and the resolved goal ids of the
    # reloaded mappings cover exactly the frozen goal set.
    root = init_project(tmp_path)
    reload_full_inventory(root)
    registered_requirements = sorted(
        (root / "requirements").glob("*.json"), key=lambda p: p.name
    )
    frozen_plan = _load_yaml(PLAN_V1_YAML)
    assert [p.stem for p in registered_requirements] == sorted(
        frozen_plan["requirement_ids"]
    )
    goal_ids = set()
    for frozen in _frozen_inventory()["items"]:
        goal_ids.update(resolve_goal_refs(frozen["requirement_mapping"]))
    # 18 goals are item-mapped by the frozen inventory; the remaining two
    # -- GOAL-AUD-001 (completeness audit, applies to the whole inventory
    # per INVENTORY.yaml requirement_mapping_policy) and GOAL-EXE-90
    # (integration) -- carry no item mapping but are covered by the plan.
    assert goal_ids == set(frozen_plan["goal_ids"]) - {
        "GOAL-AUD-001",
        "GOAL-EXE-90",
    }
    assert {"GOAL-AUD-001", "GOAL-EXE-90"} <= set(frozen_plan["goal_ids"])
    for frozen in _frozen_inventory()["items"]:
        assert not {"GOAL-AUD-001", "GOAL-EXE-90"} & set(
            resolve_goal_refs(frozen["requirement_mapping"])
        ), f"item {frozen['item_id']} maps to an unmapped-only goal"


# ---------------------------------------------------------------------------
# AC-03 (b): the reload is deterministic (byte-identical replay)
# ---------------------------------------------------------------------------


def test_fdm201_reload_deterministic_double_run(tmp_path):
    # Same frozen input -> same state: two fresh workspaces produce
    # identical audit records and byte-identical registry trees.
    def run(workspace: Path) -> tuple:
        root = init_project(workspace)
        reload_full_inventory(root)
        return root, audit_inventory_registry(root)

    first_root, first = run(tmp_path / "first")
    second_root, second = run(tmp_path / "second")
    assert first == second
    assert tree_bytes(first_root) == tree_bytes(second_root)


# ---------------------------------------------------------------------------
# AC-03 (c): the full goal family reloads and freeze round-trips
# ---------------------------------------------------------------------------


def test_fdm201_reload_goal_family_freezes_the_frozen_verdict(tmp_path):
    # AC-03: the reloaded state -- full inventory plus the full 20-goal
    # contract family -- freezes to a Plan v1 whose embedded inventory
    # audit is the frozen 82/82 PASS, at the frozen timestamp, with the
    # whole family frozen by the freeze and the plan reading back
    # identically through the plan registry.
    root = execute_reload(tmp_path)
    result = freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    frozen = result.frozen_plan
    assert frozen.status is PlanStatus.FROZEN
    assert frozen.version == "v1"
    assert frozen.frozen_at == FROZEN_TIMESTAMP
    frozen_goal_ids = set(_load_yaml(PLAN_V1_YAML)["goal_ids"])
    assert len(frozen_goal_ids) == 20
    # The reloaded plan covers the 18 item-mapped goals; the audit goal
    # (GOAL-AUD-001) and the integration goal (GOAL-EXE-90) carry no item
    # mapping, so they enter the plan's goal set via the frozen goal
    # family instead -- the whole family is frozen regardless.
    assert set(frozen.goal_ids) == frozen_goal_ids - {"GOAL-AUD-001", "GOAL-EXE-90"}
    assert {"GOAL-AUD-001", "GOAL-EXE-90"} <= {
        g.goal_id for g in result.goals
    }
    assert frozen.requirement_ids == sorted(
        _load_yaml(PLAN_V1_YAML)["requirement_ids"]
    )
    assert len(frozen.requirement_ids) == 82
    audit_view = frozen.inventory_audit
    assert audit_view.formally_reported_items == 82
    assert audit_view.mapped_items == 82
    assert audit_view.unmapped_items == 0
    assert audit_view.ambiguous_items == 0
    assert audit_view.coverage == 1.0
    assert audit_view.status is AuditStatus.PASS
    # The whole registered goal-contract family (all 20 frozen goals) is
    # frozen by the freeze; the plan's goal set is the 18 item-mapped
    # subset (GOAL-AUD-001 and GOAL-EXE-90 carry no item mapping).
    assert {g.goal_id for g in result.goals} == frozen_goal_ids
    assert all(g.frozen for g in result.goals)
    assert len(result.acceptance) == 20
    assert all(a.frozen for a in result.acceptance)
    assert len(result.analysis_protocols) == 10
    assert all(a.frozen for a in result.analysis_protocols)
    assert len(result.closure_contracts) == 4
    assert all(c.frozen for c in result.closure_contracts)
    assert result.frozen_plan == read_plan(root, "v1")
    # Immutable: a second freeze of v1 is rejected, nothing rewritten.
    with pytest.raises(PlanAlreadyFrozenError):
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)


def test_fdm201_reload_goal_family_values_reload_verbatim(tmp_path):
    # The representative GOAL-EXE-50 family reloads its frozen values
    # verbatim: title, unit process, objective, the 8 requirement ids,
    # the 3 dependency edges, replication policy, analysis protocol and
    # closure contract references, resources and assumptions.
    root = init_project(tmp_path)
    reload_full_inventory(root)
    register_goal(root, make_goal(_frozen_goal(GOAL_ID)))
    register_acceptance(root, make_acceptance(_frozen_goal(GOAL_ID)))
    stored = _load_yaml(root / "goals" / f"{GOAL_ID}.json")
    frozen = _frozen_goal(GOAL_ID)
    assert stored["title"] == frozen["title"]
    assert stored["unit_process_type"] == frozen["unit_process_type"]
    assert stored["objective"] == frozen["objective"]
    assert stored["requirement_ids"] == frozen["requirement_ids"]
    assert [d["goal_id"] for d in stored["dependencies"]] == [
        d["goal_id"] for d in frozen["dependencies"]
    ]
    assert stored["replication"]["independent_required"] == frozen["replication"][
        "independent_required"
    ]
    assert stored["replication"]["minimum_n"] == frozen["replication"]["minimum_n"]
    assert stored["replication"]["planned_n_policy"] == frozen["replication"][
        "planned_n_policy"
    ]
    assert stored["analysis_protocol_ref"] == frozen["analysis_protocol_ref"]
    assert stored["closure_contract_ref"] == frozen["closure_contract_ref"]
    assert stored["resource_ids"] == frozen["resource_ids"]
    assert stored["assumption_ids"] == frozen["assumption_ids"]
    assert stored["acceptance"]["criteria_ref"] == f"ACC-{GOAL_ID}"


def _frozen_goal(goal_id: str) -> dict:
    return next(
        g for g in _frozen_goals()["goals"] if g["goal_id"] == goal_id
    )


def test_fdm201_reload_freeze_is_deterministic_across_workspaces(tmp_path):
    # Same frozen input -> same freeze: byte-identical frozen plan and
    # byte-identical full state trees across fresh workspaces.
    first_root = execute_reload(tmp_path / "first")
    second_root = execute_reload(tmp_path / "second")
    first = freeze_plan(
        first_root, build_plan_v1(first_root), timestamp=FROZEN_AT
    )
    second = freeze_plan(
        second_root, build_plan_v1(second_root), timestamp=FROZEN_AT
    )
    assert first.frozen_plan == second.frozen_plan
    assert (
        (first_root / "plans" / "v1.json").read_bytes()
        == (second_root / "plans" / "v1.json").read_bytes()
    )
    assert tree_bytes(first_root) == tree_bytes(second_root)


# ---------------------------------------------------------------------------
# AC-03 (d): frozen facts the registry API cannot express are asserted
# directly (documented API gaps; no source change)
# ---------------------------------------------------------------------------


def test_fdm201_frozen_goal_record_direct_assertions():
    # The registry GoalContract model cannot express the frozen goals.yaml
    # inline acceptance-criteria text (its acceptance model is a
    # criteria_ref id) -- the G05 finding from PR #75. The frozen record
    # is therefore asserted directly: GOAL-EXE-50 is frozen at v1 with
    # the four inline AC-01..AC-04 texts and the frozen timestamp.
    frozen = _frozen_goal(GOAL_ID)
    assert frozen["version"] == "v1"
    assert frozen["frozen"] is True
    assert frozen["frozen_at"] == FROZEN_TIMESTAMP
    assert frozen["title"] == "Single-component C3H6/C2H4 adsorption isotherms"
    assert frozen["unit_process_type"] == "measurement"
    assert len(frozen["acceptance_criteria"]) == 4
    assert frozen["acceptance_criteria"][0].startswith("AC-01:")
    assert frozen["acceptance_criteria"][3].startswith("AC-04:")
    assert frozen["requirement_ids"] == [f"INV-030{i}" for i in range(1, 9)]
    dep_ids = [d["goal_id"] for d in frozen["dependencies"]]
    assert dep_ids == ["GOAL-EXE-20", "GOAL-EXE-30", "GOAL-EXE-40"]
    assert frozen["replication"]["independent_required"] is True
    assert frozen["replication"]["minimum_n"] == 2
    assert frozen["analysis_protocol_ref"] == "ANL-030"
    assert frozen["closure_contract_ref"] == "CC-EXPERIMENT"
    assert frozen["resource_ids"] == ["RES-015"]
    assert len(frozen["assumption_ids"]) == 5
    # Every execution goal of the frozen DAG carries inline acceptance
    # text (the same unexpressible shape), frozen at the frozen version.
    for g in _frozen_goals()["goals"]:
        assert g["acceptance_criteria"], f"goal {g['goal_id']} has no AC text"
        assert g["version"] == "v1"
        assert g["frozen"] is True


def test_fdm201_frozen_project_record_reloads_verbatim(tmp_path):
    # AC-03: the frozen project record's core fields reload through the
    # real initialize_project API with the frozen identity and timestamp;
    # the frozen file itself is asserted directly for the benchmark
    # fields the registry project record does not carry (DOI, benchmark
    # id, domain pack, source records).
    frozen = _load_yaml(PROJECT_YAML)
    assert frozen["project_id"] == FROZEN_PROJECT_ID
    assert frozen["primary_target"]["doi"] == DOI
    assert frozen["primary_target"]["identifier"] == DOI
    assert frozen["domain_pack"] == "materials-chemistry"
    assert frozen["project_phase"] == "SOURCE_ACQUISITION"
    assert frozen["current_plan_version"] == "0.1"
    assert frozen["created_at"] == FROZEN_TIMESTAMP
    assert frozen["updated_at"] == FROZEN_TIMESTAMP
    assert frozen["source_set"]["record_count"] == 13
    root = init_project(tmp_path)
    project_record = _load_yaml(root / "project.yaml")
    assert project_record["project_id"] == FROZEN_PROJECT_ID
    assert project_record["primary_target"]["doi"] == DOI
    assert project_record["primary_target"]["identifier"] == DOI
    # The registry project record uses the registry's own initial plan
    # version ("v1-draft"); the frozen "0.1" is the benchmark's version
    # vocabulary, asserted on the frozen file above.
    assert project_record["current_plan_version"] == INITIAL_PLAN_VERSION


def test_fdm201_assumptions_register_asserted_directly_no_register_api():
    # There is no register_assumption API in the registry (the frozen
    # assumptions register is a benchmark planning artifact, not a
    # registry object family); AC-03 therefore asserts the register
    # through the frozen file and the core assumption schema (see the
    # planning-benchmark acceptance file for the per-entry assertions).
    assumptions = _load_yaml(BENCHMARK_ROOT / "plans" / "assumptions.yaml")
    entries = assumptions["assumptions"]
    assert len(entries) == 46
    assert all("assumption_id" in a and "classification" in a for a in entries)
    assert {a["classification"] for a in entries} <= {
        "A0_TECHNICAL_DEFAULT",
        "A1_METHODOLOGICAL_DEFAULT",
        "A2_SCIENTIFIC_ASSUMPTION",
    }
    # The plan's assumption coverage is asserted against the plan file.
    plan = _load_yaml(PLAN_V1_YAML)
    assert set(plan["assumption_ids"]) == {a["assumption_id"] for a in entries}
