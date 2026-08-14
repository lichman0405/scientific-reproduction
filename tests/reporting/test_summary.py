"""Final outcome and method-reproducibility summary tests (DEV-M13-G03).

Every test name contains "summary" so ``python -m pytest -q tests/reporting
-k summary`` selects the whole suite. The ``ac01``/``ac02``/``ac03``
sections map one-to-one to the acceptance criteria of DEV-M13-G03:

* ``ac01`` -- reporting reflects the Core aggregation exactly: the summary
  is a deterministic rendering of the already-evaluated records
  (``ReproductionRequirement.outcome`` / ``method_reproducibility`` through
  the real ``planning.inventory`` API) and consumes the Core aggregation
  results (``core.rules.outcome``) -- the rendered outcome values are the
  recorded values and the aggregation's own values, with no transformation
  of the outcome values themselves;
* ``ac02`` -- ``RequirementOutcome`` / ``ReproductionOutcome`` /
  ``MethodReproducibility`` values are rendered verbatim and never
  conflated with ``project_phase`` (``04-PROJECT-LIFECYCLE.md`` section 1:
  the phase answers "where is the workflow now?", the outcome answers
  "what is the final scientific reproduction conclusion?");
* ``ac03`` -- recovery levels are summarized from the recorded state
  (``08-STRICT-RECOVERY-CLOSURE.md`` sections 1-2): Requirements recorded
  ``REPRODUCED_WITH_RECOVERY``, Goals recorded on the RECOVERY /
  METHOD_REDESIGN tracks, the recorded closure decisions and the recorded
  per-Requirement method-reproducibility ratings -- counted and listed,
  never computed.

The deterministic path mirrors ``reporting_helpers``: every fixture uses
fixed identities/timestamps, so all records are deterministic. The
workspaces are installed through the real registration APIs (nothing is
mocked); ``reporting_helpers`` is imported read-only.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from reporting_helpers import (
    GOAL_ID,
    INVENTORY_ID,
    REQUIREMENT_ID,
    init_project,
    install_valid_chain,
    make_goal,
    make_inventory_item,
)

from scientific_reproduction.core.models import (
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Criticality,
    GoalTrack,
    MethodReproducibility,
    ProjectPhase,
    ReproductionOutcome,
    ReproductionRequirement,
    RequirementOutcome,
)
from scientific_reproduction.core.rules.outcome import (
    RequirementOutcomeRecord,
    aggregate_project_outcome,
)
from scientific_reproduction.planning.init import read_project_state
from scientific_reproduction.planning.inventory import (
    list_requirements,
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    register_closure_contract,
    register_goal,
)
from scientific_reproduction.reporting.summary import (
    OutcomeSummary,
    SummaryCorruptError,
    SummaryError,
    SummaryNotInitializedError,
    build_summary,
)

# ---------------------------------------------------------------------------
# Test-local fixtures (deterministic, installed through the real APIs)
# ---------------------------------------------------------------------------


def make_closure_contract(
    closure_id: str = "CLS-001",
    *,
    closure_allowed: bool = True,
) -> ClosureContract:
    """A schema-valid closure contract recording the closure decision."""
    return ClosureContract(
        closure_id=closure_id,
        frozen=True,
        statistical_sufficiency={"basis": "recorded"},
        execution_validity={"basis": "recorded"},
        diagnosis={"basis": "recorded"},
        recovery=ClosureRecovery(
            eligibility_rule={"reliability_min": 3, "directness_min": 2},
            eligible_hypotheses_total=2,
            tested_or_ruled_out=2,
            remaining=0,
        ),
        literature=ClosureLiterature(
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
            required_zero_novelty_cycles=2,
        ),
        closure_allowed=closure_allowed,
    )


def make_summary_requirement(
    requirement_id: str = REQUIREMENT_ID,
    *,
    criticality: Criticality = Criticality.CRITICAL,
    outcome: RequirementOutcome = RequirementOutcome.REPRODUCED,
    method_reproducibility: MethodReproducibility | None = None,
) -> ReproductionRequirement:
    """Build a schema-valid requirement with compact defaults.

    Mirrors ``reporting_helpers.make_requirement`` but allows the full
    ``RequirementOutcome`` / ``Criticality`` vocabulary (the helper fixes
    ``outcome`` to ``REPRODUCED``), so the summary tests can pin every
    recorded outcome value (AC-02).
    """
    return ReproductionRequirement(
        requirement_id=requirement_id,
        statement="Batch-level uptake must be reproduced within tolerance",
        inventory_items=[INVENTORY_ID],
        criticality=criticality,
        goal_ids=[GOAL_ID],
        outcome=outcome,
        method_reproducibility=method_reproducibility,
    )


def install_summary_state(
    root: Path,
    *requirements: ReproductionRequirement,
    goals: tuple = (),
    closures: tuple = (),
) -> None:
    """Install a deterministic summary workspace through the real APIs.

    Registers, in authoring order: the project (``reporting_helpers``
    deterministic init), the inventory item the requirements map, the
    given requirements (``planning.inventory.register_requirement``), the
    given goals (``planning.plan.register_goal``) and the given closure
    contracts (``planning.plan.register_closure_contract``).
    """
    init_project(root)
    register_inventory_item(root, make_inventory_item())
    for requirement in requirements:
        register_requirement(root, requirement)
    for goal in goals:
        register_goal(root, goal)
    for closure in closures:
        register_closure_contract(root, closure)


# ---------------------------------------------------------------------------
# ac01 -- reporting reflects the Core aggregation exactly
# ---------------------------------------------------------------------------


def test_summary_renders_recorded_outcome_verbatim_ac01(tmp_path: Path) -> None:
    """The summary renders the recorded requirement outcome exactly (AC-01)."""
    install_valid_chain(tmp_path)
    summary = build_summary(tmp_path)

    assert isinstance(summary, OutcomeSummary)
    assert summary.project_id == read_project_state(tmp_path).project_id
    assert [entry.requirement_id for entry in summary.requirements] == [
        REQUIREMENT_ID
    ]
    entry = summary.requirements[0]
    # the rendered outcome is the recorded enum value, verbatim
    assert entry.outcome is RequirementOutcome.REPRODUCED
    assert entry.outcome.value == "REPRODUCED"
    assert entry.to_dict()["outcome"] == "REPRODUCED"
    assert entry.criticality is Criticality.CRITICAL
    # an unrated requirement renders the Core input model's canonical state
    # (never an invented rating)
    assert entry.method_reproducibility is MethodReproducibility.UNDETERMINED
    # the summary equals a deterministic rendering of the recorded state
    assert summary.to_canonical_json() == json.dumps(
        summary.to_dict(), indent=2, sort_keys=True
    ) + "\n"


def test_summary_project_outcome_equals_core_aggregation_ac01(
    tmp_path: Path,
) -> None:
    """The project-level outcome is the exact Core aggregation result (AC-01)."""
    install_valid_chain(tmp_path)
    register_requirement(
        tmp_path,
        make_summary_requirement(
            requirement_id="REQ-002",
            criticality=Criticality.SUPPORTING,
            outcome=RequirementOutcome.NOT_REPRODUCED,
        ),
    )
    register_closure_contract(tmp_path, make_closure_contract())

    summary = build_summary(tmp_path)

    # consume the Core aggregation directly over the same recorded records
    records = tuple(
        RequirementOutcomeRecord.from_reproduction_requirement(requirement)
        for requirement in list_requirements(tmp_path)
    )
    assessment = aggregate_project_outcome(records, closure_allowed=True)
    assert summary.reproduction_outcome is assessment.outcome
    assert summary.reproduction_outcome is ReproductionOutcome.PARTIALLY_REPRODUCED
    assert summary.outcome_matched_rule_id == assessment.matched_rule_id == "R-PRJ-2"
    assert summary.outcome_ruleset_version == assessment.ruleset_version
    assert summary.outcome_blocking_reasons == assessment.blocking_reasons


def test_summary_open_outcome_rendered_as_recorded_ac01(tmp_path: Path) -> None:
    """An undecided requirement renders OPEN; nothing is computed (AC-01)."""
    install_valid_chain(tmp_path)
    register_requirement(
        tmp_path,
        make_summary_requirement(
            requirement_id="REQ-002",
            outcome=RequirementOutcome.OPEN,
        ),
    )

    summary = build_summary(tmp_path)
    by_id = {entry.requirement_id: entry for entry in summary.requirements}
    assert by_id["REQ-002"].outcome is RequirementOutcome.OPEN
    assert by_id["REQ-002"].to_dict()["outcome"] == "OPEN"
    # the Core aggregation gates determination (R-REQOUT-5 -> R-PRJ-UND-1);
    # the reporting layer never decides anything itself
    assert summary.reproduction_outcome is ReproductionOutcome.UNDETERMINED
    assert summary.outcome_matched_rule_id == "R-PRJ-UND-1"
    assert "REQ-002" in summary.outcome_blocking_reasons[0]


def test_summary_canonical_json_deterministic_ac01(tmp_path: Path) -> None:
    """Identical recorded state yields byte-identical canonical JSON (AC-01)."""
    install_valid_chain(tmp_path)
    first = build_summary(tmp_path).to_canonical_json()
    second = build_summary(tmp_path).to_canonical_json()

    assert first == second
    data = json.loads(first)
    # canonical: sorted keys, one JSON document with a trailing newline
    assert list(data) == sorted(data)
    assert first.endswith("\n")


# ---------------------------------------------------------------------------
# ac02 -- outcome vocabularies rendered verbatim, never conflated with
#         project_phase
# ---------------------------------------------------------------------------


def test_summary_pins_every_outcome_value_verbatim_ac02(tmp_path: Path) -> None:
    """Every RequirementOutcome value renders verbatim (AC-02)."""
    install_valid_chain(tmp_path)
    pinned = [
        RequirementOutcome.REPRODUCED,
        RequirementOutcome.REPRODUCED_WITH_RECOVERY,
        RequirementOutcome.NOT_REPRODUCED,
        RequirementOutcome.INCONCLUSIVE,
        RequirementOutcome.OPEN,
    ]
    for offset, outcome in enumerate(pinned, start=2):
        register_requirement(
            tmp_path,
            make_summary_requirement(
                requirement_id=f"REQ-{offset:03d}",
                outcome=outcome,
            ),
        )

    summary = build_summary(tmp_path)
    by_id = {entry.requirement_id: entry for entry in summary.requirements}
    # the pinned requirement records render exactly their enum values
    for offset, outcome in enumerate(pinned, start=2):
        entry = by_id[f"REQ-{offset:03d}"]
        assert entry.outcome is outcome
        assert entry.to_dict()["outcome"] == outcome.value
    assert by_id[REQUIREMENT_ID].outcome is RequirementOutcome.REPRODUCED

    # project_phase is rendered in its own section with its own vocabulary
    assert isinstance(summary.project_phase, ProjectPhase)
    assert summary.project_phase is ProjectPhase.INITIALIZING
    phase_values = {phase.value for phase in ProjectPhase}
    outcome_values = {outcome.value for outcome in RequirementOutcome}
    assert phase_values.isdisjoint(outcome_values)
    # the rendered phase value is never an outcome value, and vice versa
    assert summary.to_dict()["project_phase"] not in outcome_values
    assert all(
        entry.to_dict()["outcome"] not in phase_values for entry in summary.requirements
    )


def test_summary_project_outcomes_rendered_verbatim_ac02(tmp_path: Path) -> None:
    """Every ReproductionOutcome value renders verbatim (AC-02)."""
    cases = [
        # (extra requirements, closure_allowed, expected outcome, rule)
        ([], True, ReproductionOutcome.FULLY_REPRODUCED, "R-PRJ-1"),
        (
            [
                (
                    "REQ-002",
                    Criticality.SUPPORTING,
                    RequirementOutcome.NOT_REPRODUCED,
                )
            ],
            True,
            ReproductionOutcome.PARTIALLY_REPRODUCED,
            "R-PRJ-2",
        ),
        (
            [("REQ-002", Criticality.CRITICAL, RequirementOutcome.NOT_REPRODUCED)],
            True,
            ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE,
            "R-PRJ-3",
        ),
        (
            [("REQ-002", Criticality.REQUIRED, RequirementOutcome.INCONCLUSIVE)],
            True,
            ReproductionOutcome.INCONCLUSIVE,
            "R-PRJ-4",
        ),
    ]
    phase_values = {phase.value for phase in ProjectPhase}
    for index, (extras, allowed, expected, rule) in enumerate(cases):
        root = tmp_path / f"case-{index}"
        install_valid_chain(root)
        for requirement_id, criticality, outcome in extras:
            register_requirement(
                root,
                make_summary_requirement(
                    requirement_id=requirement_id,
                    criticality=criticality,
                    outcome=outcome,
                ),
            )
        register_closure_contract(root, make_closure_contract(closure_allowed=allowed))

        summary = build_summary(root)
        assert summary.reproduction_outcome is expected
        assert summary.outcome_matched_rule_id == rule
        # the rendered outcome value is the enum value, verbatim (AC-02)
        assert summary.to_dict()["reproduction_outcome"] == expected.value
        # an outcome value is never a project_phase value
        assert expected.value not in phase_values
        assert isinstance(summary.project_phase, ProjectPhase)


def test_summary_method_reproducibility_verbatim_ac02(tmp_path: Path) -> None:
    """Method reproducibility ratings render verbatim (AC-02)."""
    install_summary_state(
        tmp_path,
        make_summary_requirement(
            requirement_id="REQ-001",
            outcome=RequirementOutcome.REPRODUCED,
            method_reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE,
        ),
        make_summary_requirement(
            requirement_id="REQ-002",
            outcome=RequirementOutcome.REPRODUCED_WITH_RECOVERY,
            method_reproducibility=MethodReproducibility.ONLY_REPRODUCIBLE_AFTER_REDESIGN,
        ),
    )

    summary = build_summary(tmp_path)
    by_id = {entry.requirement_id: entry for entry in summary.requirements}
    assert (
        by_id["REQ-001"].method_reproducibility
        is MethodReproducibility.DIRECTLY_REPRODUCIBLE
    )
    assert (
        by_id["REQ-002"].method_reproducibility
        is MethodReproducibility.ONLY_REPRODUCIBLE_AFTER_REDESIGN
    )
    assert (
        by_id["REQ-002"].to_dict()["method_reproducibility"]
        == MethodReproducibility.ONLY_REPRODUCIBLE_AFTER_REDESIGN.value
    )
    # the aggregate is the Core aggregation's own rating (worst terminal:
    # ONLY_REPRODUCIBLE_AFTER_REDESIGN, R-MR-3)
    assert (
        summary.method_reproducibility
        is MethodReproducibility.ONLY_REPRODUCIBLE_AFTER_REDESIGN
    )
    assert summary.method_matched_rule_id == "R-MR-3"


# ---------------------------------------------------------------------------
# ac03 -- recovery levels are summarized from the recorded state
# ---------------------------------------------------------------------------


def test_summary_recovery_state_from_recorded_records_ac03(
    tmp_path: Path,
) -> None:
    """Recovery state is summarized from the recorded records (AC-03)."""
    install_summary_state(
        tmp_path,
        make_summary_requirement(
            requirement_id="REQ-001",
            outcome=RequirementOutcome.REPRODUCED,
            method_reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE,
        ),
        make_summary_requirement(
            requirement_id="REQ-002",
            outcome=RequirementOutcome.REPRODUCED_WITH_RECOVERY,
            method_reproducibility=MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY,
        ),
        goals=(
            # ``reporting_helpers.make_goal`` fixes the track to
            # STRICT_REPRODUCTION, so the recorded track is swapped on the
            # helper's default goal contract (schema-valid, deterministic).
            replace(make_goal(goal_id="GOAL-001"), track=GoalTrack.RECOVERY),
            replace(
                make_goal(goal_id="GOAL-002"), track=GoalTrack.METHOD_REDESIGN
            ),
        ),
        closures=(make_closure_contract(),),
    )

    summary = build_summary(tmp_path)
    recovery = summary.recovery

    # recorded recovery closures, tracks and closure decisions, verbatim
    assert recovery.recovered_requirements == ("REQ-002",)
    assert recovery.recovered_count == 1
    assert recovery.recovery_goals == ("GOAL-001",)
    assert recovery.recovery_goal_count == 1
    assert recovery.method_redesign_goals == ("GOAL-002",)
    assert recovery.method_redesign_goal_count == 1
    assert recovery.recorded_closure_decisions == (("CLS-001", True),)

    # Scenario B (18-TEST-AND-ACCEPTANCE-PLAN.md): the recovered
    # requirement renders REPRODUCED_WITH_RECOVERY verbatim and the method
    # reproducibility aggregates lower than direct reproducibility
    by_id = {entry.requirement_id: entry for entry in summary.requirements}
    assert by_id["REQ-002"].outcome is RequirementOutcome.REPRODUCED_WITH_RECOVERY
    assert (
        by_id["REQ-002"].method_reproducibility
        is MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY
    )
    assert summary.method_matched_rule_id == "R-MR-3"
    assert (
        summary.method_reproducibility
        is MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY
    )
    # both requirements close REPRODUCED (recovery counts as reproduced in
    # the Core aggregation, R-REQOUT-2): FULLY_REPRODUCED
    assert summary.reproduction_outcome is ReproductionOutcome.FULLY_REPRODUCED
    assert summary.outcome_matched_rule_id == "R-PRJ-1"


def test_summary_no_recovery_nothing_invented_ac03(tmp_path: Path) -> None:
    """A project with no recorded recovery renders an empty summary (AC-03)."""
    install_summary_state(tmp_path, make_summary_requirement())

    summary = build_summary(tmp_path)
    assert summary.recovery.recovered_requirements == ()
    assert summary.recovery.recovered_count == 0
    assert summary.recovery.recovery_goals == ()
    assert summary.recovery.method_redesign_goals == ()
    assert summary.recovery.recorded_closure_decisions == ()
    # nothing recorded -> nothing invented: the aggregation stays total
    assert summary.reproduction_outcome is ReproductionOutcome.FULLY_REPRODUCED
    assert summary.outcome_matched_rule_id == "R-PRJ-1"


def test_summary_closure_allowed_consumed_for_critical_not_reproduced_ac03(
    tmp_path: Path,
) -> None:
    """A recorded satisfied closure contract allows the negative closure."""
    install_summary_state(
        tmp_path,
        make_summary_requirement(
            requirement_id="REQ-001",
            criticality=Criticality.CRITICAL,
            outcome=RequirementOutcome.NOT_REPRODUCED,
        ),
        closures=(make_closure_contract(closure_allowed=True),),
    )

    summary = build_summary(tmp_path)
    assert summary.recovery.recorded_closure_decisions == (("CLS-001", True),)
    assert (
        summary.reproduction_outcome
        is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    )
    assert summary.outcome_matched_rule_id == "R-PRJ-3"


def test_summary_closure_not_allowed_blocks_negative_closure_ac03(
    tmp_path: Path,
) -> None:
    """A recorded not-satisfied closure decision blocks determination."""
    install_summary_state(
        tmp_path,
        make_summary_requirement(
            requirement_id="REQ-001",
            criticality=Criticality.CRITICAL,
            outcome=RequirementOutcome.NOT_REPRODUCED,
        ),
        closures=(make_closure_contract(closure_allowed=False),),
    )

    summary = build_summary(tmp_path)
    assert summary.recovery.recorded_closure_decisions == (("CLS-001", False),)
    assert summary.reproduction_outcome is ReproductionOutcome.UNDETERMINED
    assert summary.outcome_matched_rule_id == "R-PRJ-UND-2"


def test_summary_conflicting_closure_decisions_rendered_not_computed_ac03(
    tmp_path: Path,
) -> None:
    """Conflicting recorded closure decisions render as recorded (AC-03)."""
    install_summary_state(
        tmp_path,
        make_summary_requirement(
            requirement_id="REQ-001",
            criticality=Criticality.CRITICAL,
            outcome=RequirementOutcome.NOT_REPRODUCED,
        ),
        closures=(
            make_closure_contract(closure_id="CLS-001", closure_allowed=True),
            make_closure_contract(closure_id="CLS-002", closure_allowed=False),
        ),
    )

    summary = build_summary(tmp_path)
    # both recorded decisions stay visible, sorted by closure_id
    assert summary.recovery.recorded_closure_decisions == (
        ("CLS-001", True),
        ("CLS-002", False),
    )
    # the recorded state is ambiguous: the aggregation consumes the
    # unassessed (None) state and blocks determination (R-PRJ-UND-3)
    assert summary.reproduction_outcome is ReproductionOutcome.UNDETERMINED
    assert summary.outcome_matched_rule_id == "R-PRJ-UND-3"


# ---------------------------------------------------------------------------
# Structural boundaries
# ---------------------------------------------------------------------------


def test_summary_errors_are_value_errors() -> None:
    """The module errors follow the ValueError-subclass convention."""
    assert issubclass(SummaryError, ValueError)
    assert issubclass(SummaryNotInitializedError, SummaryError)
    assert issubclass(SummaryCorruptError, SummaryError)


def test_summary_not_initialized_error(tmp_path: Path) -> None:
    """Summarizing an uninitialized workspace raises a stable error."""
    with pytest.raises(SummaryNotInitializedError):
        build_summary(tmp_path)


def test_summary_type_error() -> None:
    """Non-path roots are rejected at the public boundary."""
    with pytest.raises(TypeError):
        build_summary(42)  # type: ignore[arg-type]


def test_summary_corrupt_requirement_record_raises(tmp_path: Path) -> None:
    """A corrupt stored requirement record fails loudly (never silently)."""
    install_valid_chain(tmp_path)
    record = tmp_path / "requirements" / f"{REQUIREMENT_ID}.json"
    record.write_text("{not json", encoding="utf-8")
    with pytest.raises(SummaryCorruptError):
        build_summary(tmp_path)


def test_summary_sorted_deterministic_ac01(tmp_path: Path) -> None:
    """Requirements render sorted by requirement_id (deterministic)."""
    install_summary_state(
        tmp_path,
        make_summary_requirement(requirement_id="REQ-003", outcome=RequirementOutcome.REPRODUCED),
        make_summary_requirement(requirement_id="REQ-001", outcome=RequirementOutcome.REPRODUCED),
        make_summary_requirement(requirement_id="REQ-002", outcome=RequirementOutcome.REPRODUCED),
    )

    summary = build_summary(tmp_path)
    assert [entry.requirement_id for entry in summary.requirements] == [
        "REQ-001",
        "REQ-002",
        "REQ-003",
    ]
