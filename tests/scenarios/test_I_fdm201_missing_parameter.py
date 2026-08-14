"""FDM-201 simulated scenario I -- missing critical method parameter (DEV-M12-G06).

Scenario I is the frozen acceptance fixture of
``18-TEST-AND-ACCEPTANCE-PLAN.md``: a critical method parameter is
**absent from paper/SI/related sources** -- the GCMC force field,
partial charges and mixing rules of the FDM-201 adsorption simulations
are reported only in the SI, which is not downloaded at registration
(``AMB-0006``; the frozen ``ASM-FDM-0605`` register entry). Expected
(frozen acceptance, AC-04 of DEV-M12-G06):

* the missing parameters enter the **Assumption Registry** and are
  classified **A2** per the semantics of the frozen register entry
  ``ASM-FDM-0605`` (``A2_SCIENTIFIC_ASSUMPTION``,
  ``DISQUALIFIES_PURE_STRICT``);
* an A2 assumption **blocks pure STRICT labeling** (the real
  ``evaluate_strict_label`` rule table: ``R-STRICT-2`` -> ``NOT_STRICT``,
  ``R-EFF-1`` -> ``DISQUALIFIES_PURE_STRICT``) -- the reproduction
  cannot be labeled pure strict while the settings are unresolved;
* **optional author contact remains disabled** unless a Human Gate is
  opened: the frozen research role contract forbids the practice
  ``author_contact_without_human_gate``, the permission matrix grants
  ``HUMAN_GATE_OPEN`` to the Supervisor only, and the execution path
  never opens a gate -- the gate record (``EXTERNAL_CONTACT_GATE``,
  ``OPEN``) enters the workspace ``human-gates/`` registry only when the
  Supervisor opens it, over the frozen ``RES-001`` primary-paper
  resource (``human_gate_required: true``).

Everything runs on the real machinery: the real
``domain_packs.materials_chemistry.computation`` GCMC template and its
``assumptions_for_missing_parameters`` routing (the EXISTING Assumption
Registry pathway of DEV-M11-G04, never a parallel store), the real
``core.rules.assumptions`` ``assumption_effect`` / ``evaluate_strict_label``,
the real permission matrix and the real role-contract layer, and the
real schema gate + atomic persistence (``core.schema_validation`` /
``core.atomic``) for the workspace ``assumptions/`` and ``human-gates/``
registries. All grounding values are read live from the frozen
``benchmarks/fdm201/`` register files (``plans/assumptions.yaml``,
``inventory/INVENTORY.yaml``, ``execution_packages/computation/cmp-pkg-03-gcmc.yaml``,
``resources/resources.yaml``) -- nothing is invented.

Determinism mirrors the F/G/H suites: fixed identities/timestamps,
pinned safe ids, deterministic ``generate_id`` assumption ids, no
randomness, no wall clock, no network.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from scientific_reproduction.adapters.platform.contracts.base import (
    get_role_contract,
)
from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    Assumption,
    AssumptionClassification,
    GateStatus,
    GateType,
    HumanGate,
    StrictStatusEffect,
)
from scientific_reproduction.core.permissions import (
    Action,
    Role,
    is_action_allowed,
)
from scientific_reproduction.core.rules.assumptions import (
    StrictLabel,
    assumption_effect,
    evaluate_strict_label,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    EXECUTION_STAGE,
    GcmcTemplate,
    MissingParameterRouting,
    apply_assumption_routing,
    assumptions_for_missing_parameters,
)
from scientific_reproduction.planning.init import initialize_project

#: Deterministic author/committer identity (mirrors the other scenario suites).
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI of the one-paper project (17-FDM201-REFERENCE-CASE.md).
DOI = "10.1039/D5TA00771B"

#: The frozen benchmark register (read live, never copied into this file).
BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "fdm201"
ASSUMPTIONS_YAML = BENCHMARK_ROOT / "plans" / "assumptions.yaml"
INVENTORY_YAML = BENCHMARK_ROOT / "inventory" / "INVENTORY.yaml"
RESOURCES_YAML = BENCHMARK_ROOT / "resources" / "resources.yaml"
PACKAGE_YAML = (
    BENCHMARK_ROOT / "execution_packages" / "computation" / "cmp-pkg-03-gcmc.yaml"
)

#: The frozen Assumption Registry entry scenario I executes (verbatim
#: values from plans/assumptions.yaml; see test_I_ac04_frozen_register*).
A2_ASSIGNMENT_ID = "ASM-FDM-0605"
A2_ASSIGNMENT_PARAMETER = (
    "GCMC force field, partial charges, mixing rules, ensemble and software"
)
A2_AFFECTED_GOALS = ("GOAL-EXE-80", "GOAL-EXE-82")

#: The GCMC settings absent from the accessible sources (the SI is not
#: downloaded at registration, AMB-0006). The frozen register names the
#: same settings verbatim (``ASM-FDM-0605``); ``software``/``version``
#: are inventoried at execution (RES-010) and the ensemble is implied by
#: the GCMC template kind, so only these three remain unresolved.
GCMC_MISSING_PARAMETERS = ("force_field", "charges", "mixing_rules")

#: The frozen primary-paper resource the author-contact gate targets
#: (resources/resources.yaml; ``human_gate_required: true``).
RESOURCE_PRIMARY_PAPER = "RES-001"

#: The deterministic gate id the Supervisor opens for scenario I.
GATE_ID = generate_id("gate", "FDM-201", A2_ASSIGNMENT_ID)


# ---------------------------------------------------------------------------
# Deterministic project fixtures (self-contained: scenario tests live in
# their own directory, so the planning/domain suites' helpers are not on
# the import path)
# ---------------------------------------------------------------------------


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def make_gcmc_template(
    *, missing: tuple[str, ...] = GCMC_MISSING_PARAMETERS,
) -> GcmcTemplate:
    """The GCMC execution template of CMP-03, missing the SI settings.

    The base template records the full execution surface (software/
    version from the inventoried RES-010 settings, force-field/charges/
    mixing-rules placeholders, interaction cutoff, temperature/pressure
    grid, cycle counts, seed; engineering fixture values, never
    asserted); ``missing`` drops the settings that stay unresolved after
    CMP03-S03 extraction and must enter the Assumption Registry per
    ASM-FDM-0605.
    """
    parameters: dict[str, object] = {
        "software": "raspa",
        "software_version": "2.0.47",
        "force_field": "uff",
        "charges": "reported-in-si",
        "mixing_rules": "reported-in-si",
        "cutoff_angstrom": 12.0,
        "temperature_K": 298.0,
        "pressure_bar": 1.0,
        "equilibration_cycles": 10000,
        "production_cycles": 10000,
        "seed": 42,
    }
    for name in missing:
        parameters.pop(name, None)
    return GcmcTemplate(
        template_id="gcmc-fdm201-cmp03",
        title="FDM-201 GCMC adsorption reproduction (GOAL-EXE-82)",
        stage=EXECUTION_STAGE,
        parameters=parameters,
    )


def _canonical_json(data: dict[str, object]) -> str:
    """The registry canonical serialization (same convention as
    ``planning/inventory.py``)."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class ScenarioIResult:
    """Everything the executed scenario produced (frozen, auditable)."""

    root: Path
    routing: MissingParameterRouting
    template: GcmcTemplate


def execute_scenario_i(root: Path) -> ScenarioIResult:
    """Execute scenario I end to end.

    CMP03-S03: the GCMC settings that remain unresolved after extraction
    are routed through the real Assumption Registry pathway (A2 per the
    frozen ASM-FDM-0605 semantics) and every routed record enters the
    workspace ``assumptions/`` registry through the real schema gate and
    atomic canonical persistence -- the house registry pattern of
    ``planning/inventory.py``. The execution path never attempts author
    contact and never opens a Human Gate (optional contact stays
    disabled; see the Human Gate section).
    """
    template = make_gcmc_template()
    routing = assumptions_for_missing_parameters(
        template, affected_goal_ids=A2_AFFECTED_GOALS
    )
    registry_dir = root / "assumptions"
    registry_dir.mkdir(exist_ok=True)
    for assumption in routing.assumptions:
        validate_and_reject("assumption", assumption.to_dict())
        atomic_write(
            registry_dir / f"{assumption.assumption_id}.json",
            _canonical_json(assumption.to_dict()),
        )
    return ScenarioIResult(root=root, routing=routing, template=template)


def open_author_contact_gate(root: Path) -> HumanGate:
    """The Supervisor opens the external-contact Human Gate (the only path
    that makes optional author contact permissible; the real gate record
    enters the workspace ``human-gates/`` registry)."""
    gate = HumanGate(
        gate_id=GATE_ID,
        gate_type=GateType.EXTERNAL_CONTACT_GATE,
        status=GateStatus.OPEN,
        trigger=(
            "ASM-FDM-0605: the GCMC force field, partial charges, mixing"
            " rules, ensemble and software are reported in the SI, which is"
            " not downloaded at registration (AMB-0006); contacting the"
            " authors to resolve the settings requires an external-contact"
            " Human Gate over the primary-paper resource (RES-001,"
            " human_gate_required)"
        ),
        affected_refs=[RESOURCE_PRIMARY_PAPER],
    )
    validate_and_reject("human-gate", gate.to_dict())
    gate_dir = root / "human-gates"
    gate_dir.mkdir(exist_ok=True)
    atomic_write(gate_dir / f"{gate.gate_id}.json", _canonical_json(gate.to_dict()))
    return gate


# ---------------------------------------------------------------------------
# AC-04 (a): the frozen register grounds the scenario (live benchmark data)
# ---------------------------------------------------------------------------


def _frozen_assumptions() -> list[dict[str, object]]:
    """The frozen assumption register rows (plans/assumptions.yaml)."""
    with ASSUMPTIONS_YAML.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)["assumptions"]


def test_I_ac04_frozen_register_grounds_the_missing_parameter_assignment():
    # Scenario I is not an invented case: the frozen register already
    # classifies the missing GCMC settings as A2 -- the parameter text
    # names force field, partial charges, mixing rules, ensemble and
    # software, the effect is DISQUALIFIES_PURE_STRICT and the entry
    # traces to the inventory, the acquisition log and the reference case.
    rows = _frozen_assumptions()
    assert len(rows) == 46  # total frozen assumption register
    a2_rows = [r for r in rows if r["classification"] == "A2_SCIENTIFIC_ASSUMPTION"]
    assert len(a2_rows) == 12  # the frozen A2 set includes ASM-FDM-0605
    assignment = next(r for r in rows if r["assumption_id"] == A2_ASSIGNMENT_ID)
    assert assignment["parameter"] == A2_ASSIGNMENT_PARAMETER
    assert assignment["classification"] == "A2_SCIENTIFIC_ASSUMPTION"
    assert assignment["value"] is None
    assert assignment["strict_status_effect"] == "DISQUALIFIES_PURE_STRICT"
    assert assignment["affected_goal_ids"] == list(A2_AFFECTED_GOALS)
    assert "INV-0605" in assignment["source_refs"]
    assert assignment["rationale"]  # the A2 semantics are documented


def test_I_ac04_every_frozen_a2_registration_disqualifies_pure_strict():
    # The frozen A2 set is exactly the pure-strict blocker set: the real
    # rule engine reads every register entry back into R-EFF-1 /
    # R-STRICT-2, and the 12 A2 entries jointly force NOT_STRICT.
    rows = _frozen_assumptions()
    a2_rows = [r for r in rows if r["classification"] == "A2_SCIENTIFIC_ASSUMPTION"]
    entries = [Assumption.from_dict(row) for row in a2_rows]
    assert len(entries) == 12
    for entry in entries:
        effect = assumption_effect(entry)
        assert effect.effect is StrictStatusEffect.DISQUALIFIES_PURE_STRICT
        assert effect.rule_id == "R-EFF-1"
        assert entry.strict_status_effect is StrictStatusEffect.DISQUALIFIES_PURE_STRICT
    label = evaluate_strict_label(entries)
    assert label.label is StrictLabel.NOT_STRICT
    assert label.matched_label_rule_id == "R-STRICT-2"


def test_I_ac04_parameter_absent_from_paper_si_related_sources():
    # AC-04 scenario premise ("parameter absent from paper/SI/related
    # sources"): INV-0605 -- the force field/charges/software settings --
    # is DEFERRED-TO-EXECUTION with EXISTENCE-ONLY value status because
    # the SI is not downloaded at registration (AMB-0006), and the frozen
    # execution package CMP03-S03 registers whatever stays unresolved as
    # A2 per ASM-FDM-0605.
    with INVENTORY_YAML.open(encoding="utf-8") as handle:
        inventory = yaml.safe_load(handle)
    inv_0605 = next(it for it in inventory["items"] if it["item_id"] == "INV-0605")
    assert inv_0605["title"] == "Force-field / charges / software settings"
    assert inv_0605["status"] == "DEFERRED-TO-EXECUTION"
    assert inv_0605["value_status"] == "EXISTENCE-ONLY"
    assert "not downloaded at registration" in inv_0605["missing_reason"]
    assert "AMB-0006" in inv_0605["missing_reason"]
    with PACKAGE_YAML.open(encoding="utf-8") as handle:
        package = yaml.safe_load(handle)
    step = next(s for s in package["procedure"] if s["id"] == "CMP03-S03")
    assert "unresolved settings are registered as A2 per ASM-FDM-0605" in step[
        "action"
    ]
    assert A2_ASSIGNMENT_ID in step["trace_refs"]
    # The scenario's missing parameters are the settings the register names
    # (verbatim frozen text, normalized to the template parameter spelling).
    parameter_text = (
        A2_ASSIGNMENT_PARAMETER.lower()
        .replace("force field", "force_field")
        .replace("mixing rules", "mixing_rules")
    )
    assert all(name in parameter_text for name in GCMC_MISSING_PARAMETERS)


# ---------------------------------------------------------------------------
# AC-04 (b): Assumption Registry classification -- A2, never pure STRICT
# ---------------------------------------------------------------------------


def test_I_ac04_missing_gcmc_settings_route_to_a2_assumptions(tmp_path):
    # The unresolved GCMC settings enter the Assumption Registry through
    # the real routing: one real Assumption per missing parameter, A2
    # classified with the DISQUALIFIES_PURE_STRICT effect recorded, safe
    # deterministic ids, the affected goals of the frozen entry.
    root = init_project(tmp_path)
    scenario = execute_scenario_i(root)
    routing = scenario.routing
    assert routing.template_id == "gcmc-fdm201-cmp03"
    assert routing.missing_parameters == GCMC_MISSING_PARAMETERS
    assert len(routing.assumptions) == 3
    for assumption in routing.assumptions:
        assert assumption.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
        assert assumption.strict_status_effect is StrictStatusEffect.DISQUALIFIES_PURE_STRICT
        assert assumption.parameter in GCMC_MISSING_PARAMETERS
        assert is_valid_id(assumption.assumption_id, "assumption")
        assert assumption.affected_goal_ids == list(A2_AFFECTED_GOALS)
        assert assumption.rationale  # the registry records why the A2 applies
    assert routing.assumption_refs == tuple(
        a.assumption_id for a in routing.assumptions
    )
    # The routed template carries the assumption refs, never mutated inputs.
    routed_template = apply_assumption_routing(scenario.template, routing)
    assert routed_template.assumption_refs == routing.assumption_refs
    assert scenario.template.assumption_refs == ()


def test_I_ac04_pure_strict_labeling_is_blocked(tmp_path):
    # AC-04: an A2 assumption prevents pure strict labeling. The real
    # evaluate_strict_label over the routed set returns NOT_STRICT
    # (R-STRICT-2) -- the pure-STRICT rule R-STRICT-1 is explicitly
    # evaluated and rejected -- and every per-assumption effect is
    # R-EFF-1 / DISQUALIFIES_PURE_STRICT.
    root = init_project(tmp_path)
    scenario = execute_scenario_i(root)
    assessment = scenario.routing.strict_label_assessment
    assert assessment.label is StrictLabel.NOT_STRICT
    assert assessment.matched_label_rule_id == "R-STRICT-2"
    strict_decision = next(
        d for d in assessment.label_decisions if d.rule_id == "R-STRICT-1"
    )
    assert strict_decision.matched is False  # pure STRICT explicitly refused
    for effect in scenario.routing.effects:
        assert effect.rule_id == "R-EFF-1"
        assert effect.effect is StrictStatusEffect.DISQUALIFIES_PURE_STRICT
    # Reading the routed records back through the real API reproduces the
    # assessment exactly (the routing is the real registry pathway).
    read_back = evaluate_strict_label(scenario.routing.assumptions)
    assert read_back.label == assessment.label
    assert read_back.matched_label_rule_id == assessment.matched_label_rule_id


def test_I_ac04_a2_classification_not_mere_missingness_blocks_pure_strict(tmp_path):
    # Contrast (grounded in the frozen register): a missing force field
    # backed by the frozen A1 methodological default (ASM-A1-FF-01) stays
    # STRICT_WITH_ASSUMPTIONS (R-EFF-2 / R-STRICT-3) -- it is the A2
    # classification of ASM-FDM-0605, not the missing parameter itself,
    # that blocks pure STRICT labeling.
    template = make_gcmc_template(missing=("force_field",))
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
        source_refs=("ASM-A1-FF-01",),
    )
    assert routing.missing_parameters == ("force_field",)
    assert routing.assumptions[0].classification is (
        AssumptionClassification.A1_METHODOLOGICAL_DEFAULT
    )
    assert routing.effects[0].rule_id == "R-EFF-2"
    assert routing.effects[0].effect is StrictStatusEffect.STRICT_WITH_ASSUMPTIONS
    assert routing.strict_label_assessment.label is StrictLabel.STRICT_WITH_ASSUMPTIONS
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-3"


# ---------------------------------------------------------------------------
# AC-04 (c): the routed records live in the workspace Assumption Registry
# ---------------------------------------------------------------------------


def test_I_ac04_assumptions_registered_into_workspace_registry(tmp_path):
    # The routed records enter the workspace assumptions/ registry through
    # the real schema gate and canonical atomic persistence, and read back
    # byte-identical: the registry round-trip is exact.
    root = init_project(tmp_path)
    scenario = execute_scenario_i(root)
    registry_dir = root / "assumptions"
    stored_paths = sorted(registry_dir.glob("*.json"))
    assert [p.name[:-5] for p in stored_paths] == sorted(
        routing_refs := scenario.routing.assumption_refs
    )
    for path in stored_paths:
        record = Assumption.from_dict(json.loads(path.read_text(encoding="utf-8")))
        expected = next(
            a for a in scenario.routing.assumptions if a.assumption_id == record.assumption_id
        )
        assert record == expected
        assert path.read_text(encoding="utf-8") == _canonical_json(expected.to_dict())
        validate_and_reject("assumption", json.loads(path.read_text(encoding="utf-8")))
    assert set(routing_refs) == {
        a.assumption_id for a in scenario.routing.assumptions
    }


def test_I_ac04_assumption_registry_deterministic_across_workspaces(tmp_path):
    # Same scenario inputs -> same registry: the routed records (ids and
    # persisted bytes) are identical across fresh workspaces.
    first = execute_scenario_i(init_project(tmp_path / "first"))
    second = execute_scenario_i(init_project(tmp_path / "second"))
    assert first.routing == second.routing
    first_files = sorted((first.root / "assumptions").glob("*.json"))
    second_files = sorted((second.root / "assumptions").glob("*.json"))
    assert [f.name for f in first_files] == [f.name for f in second_files]
    for a, b in zip(first_files, second_files):
        assert a.read_bytes() == b.read_bytes()


# ---------------------------------------------------------------------------
# AC-04 (d): optional author contact stays disabled without a Human Gate
# ---------------------------------------------------------------------------


def test_I_ac04_author_contact_disabled_without_human_gate():
    # The frozen research role contract forbids author contact without a
    # Human Gate (practice + prompt prohibition), grants no gate-opening
    # action, and the permission matrix opens HUMAN_GATE_OPEN to the
    # Supervisor only -- optional contact is disabled by default.
    research = get_role_contract("research")
    assert "author_contact_without_human_gate" in research.forbidden_practices
    assert "never contact authors without a Human Gate" in research.prompt_prohibitions
    assert Action.HUMAN_GATE_OPEN not in research.allowed_actions
    supervisor = get_role_contract("supervisor")
    assert Action.HUMAN_GATE_OPEN in supervisor.allowed_actions
    for role in (
        Role.RESEARCH,
        Role.MONITOR,
        Role.EXPERIMENT_WORKER,
        Role.COMPUTATION_WORKER,
        Role.ANALYSIS_WORKER,
        Role.DIAGNOSIS_WORKER,
    ):
        assert is_action_allowed(role, Action.HUMAN_GATE_OPEN) is False, role
    assert is_action_allowed(Role.SUPERVISOR, Action.HUMAN_GATE_OPEN) is True


def test_I_ac04_human_gate_is_the_only_author_contact_path(tmp_path):
    # Execution never opens a gate: after the scenario the human-gates
    # registry holds no record (optional contact stays disabled). The
    # frozen primary-paper resource requires the gate (RES-001,
    # human_gate_required), and opening it is the Supervisor's step -- the
    # real EXTERNAL_CONTACT_GATE / OPEN record then enters the workspace
    # registry through the schema gate and reads back byte-identical.
    root = init_project(tmp_path)
    execute_scenario_i(root)
    gate_dir = root / "human-gates"
    assert gate_dir.is_dir()
    assert list(gate_dir.glob("*.json")) == []  # still disabled after execution
    with RESOURCES_YAML.open(encoding="utf-8") as handle:
        resources = yaml.safe_load(handle)["resources"]
    primary_paper = next(r for r in resources if r["resource_id"] == RESOURCE_PRIMARY_PAPER)
    assert primary_paper["human_gate_required"] is True
    # The Supervisor opens the gate (the only permitted path).
    gate = open_author_contact_gate(root)
    assert gate.gate_type is GateType.EXTERNAL_CONTACT_GATE
    assert gate.status is GateStatus.OPEN
    assert gate.affected_refs == [RESOURCE_PRIMARY_PAPER]
    stored_path = gate_dir / f"{GATE_ID}.json"
    assert stored_path.is_file()
    stored = HumanGate.from_dict(json.loads(stored_path.read_text(encoding="utf-8")))
    assert stored == gate
    assert stored_path.read_text(encoding="utf-8") == _canonical_json(gate.to_dict())
    validate_and_reject("human-gate", json.loads(stored_path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Paradigm boundaries (determinism, safe ids, frozen records)
# ---------------------------------------------------------------------------


def test_I_ac04_scenario_records_reject_mutation(tmp_path):
    # Frozen dataclasses throughout: the routing, its assumptions, the
    # template and the gate record cannot be mutated after construction.
    root = init_project(tmp_path)
    scenario = execute_scenario_i(root)
    gate = open_author_contact_gate(root)
    records = (
        scenario.routing,
        scenario.routing.assumptions[0],
        scenario.routing.strict_label_assessment,
        scenario.template,
        gate,
    )
    for record in records:
        assert is_dataclass(record)
        field_name = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, None)


def test_I_ac04_scenario_uses_safe_ids_only(tmp_path):
    # Every id on the scenario path is a safe registry id.
    root = init_project(tmp_path)
    scenario = execute_scenario_i(root)
    ids = (
        A2_ASSIGNMENT_ID,
        GATE_ID,
        RESOURCE_PRIMARY_PAPER,
        "INV-0605",
        "AMB-0006",
        *GCMC_MISSING_PARAMETERS,
        *scenario.routing.assumption_refs,
    )
    for value in ids:
        assert value not in ("", ".", "..")
        assert "/" not in value and "\\" not in value
        assert not any(char in value for char in "*?[]")
    stored = {p.name for p in (root / "assumptions").glob("*.json")}
    assert stored == {f"{a}.json" for a in scenario.routing.assumption_refs}
