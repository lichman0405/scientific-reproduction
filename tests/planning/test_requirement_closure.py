"""Tests for the sanctioned requirement closure API (issue #120).

Coverage:

  * the closure persists the outcome update to ``requirements/<id>.json``
    and appends one deterministic ``requirement.outcome.updated`` event
    that maps to the "Requirement outcome updated" git audit checkpoint;
  * every terminal outcome (R-REQOUT-1..4) closes; ``OPEN`` is rejected
    by the closure rules (R-REQOUT-5) before anything is written;
  * unknown / unregistered / unsafe requirement ids are refused;
  * argument validation (TypeError at the boundary, empty audit args);
  * optional ``method_reproducibility`` recording (schema-validated
    only, ``UNDETERMINED`` is legal);
  * exactly-once: a fully recorded closure is a no-op and is rejected --
    it must never enter the audit record a second time;
  * crash-window convergence (monitoring pattern): a closure whose
    record write landed but event append did not is healed with a
    ``replayed=True`` event;
  * re-closure between terminal outcomes chains events (the from/to
    arc documents the outcome line);
  * persist-only mode (no event log) never appends events.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from inventory_helpers import init_project, make_item, make_requirement

from scientific_reproduction.audit.git import AuditCommitRequest, map_event_to_audit
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    MethodReproducibility,
    ProjectEvent,
    ReproductionRequirement,
    RequirementOutcome,
)
from scientific_reproduction.core.rules.outcome import (
    RequirementClosureState,
)
from scientific_reproduction.planning.init import ProjectNotInitializedError
from scientific_reproduction.planning.inventory import (
    REQUIREMENT_OUTCOME_UPDATED_EVENT_TYPE,
    REQUIREMENTS_STATE_DIR,
    InvalidRegistryIdError,
    RequirementClosureError,
    RequirementNotFoundError,
    close_requirement,
    read_requirement,
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.research.state_helpers import EVENTS_STATE_DIR

#: Deterministic acting identity stamped on every closure event.
ACTOR = "supervisor"

#: Deterministic closure timestamp (events carry timestamps; the
#: requirement records themselves do not).
AT = "2026-01-02T00:00:00Z"

#: Stable closure reason.
REASON = "adsorption isotherm reproduced within tolerance"

#: Terminal outcomes of 04-PROJECT-LIFECYCLE.md section 4 and their
#: normative classification (core/rules/outcome.py R-REQOUT-1..4).
TERMINAL_OUTCOMES = (
    (RequirementOutcome.REPRODUCED, "R-REQOUT-1", RequirementClosureState.REPRODUCED),
    (
        RequirementOutcome.REPRODUCED_WITH_RECOVERY,
        "R-REQOUT-2",
        RequirementClosureState.REPRODUCED,
    ),
    (
        RequirementOutcome.NOT_REPRODUCED,
        "R-REQOUT-3",
        RequirementClosureState.NOT_REPRODUCED,
    ),
    (RequirementOutcome.INCONCLUSIVE, "R-REQOUT-4", RequirementClosureState.INCONCLUSIVE),
)


def event_log(root: Path) -> ProjectEventLog:
    """The workspace event log bound to the ``events/`` directory."""
    return ProjectEventLog(root / EVENTS_STATE_DIR)


def registered_requirement(
    root: Path, requirement_id: str = "REQ-1"
) -> ReproductionRequirement:
    """Register one item plus one requirement mapping it; return the record."""
    register_inventory_item(root, make_item("INV-MAIN-ADS-001"))
    return register_requirement(
        root,
        make_requirement(
            requirement_id,
            inventory_items=("INV-MAIN-ADS-001",),
            goal_ids=("G",),
        ),
    )


def closure_events(root: Path, requirement_id: str = "REQ-1") -> list[ProjectEvent]:
    """The recorded closure events of one requirement, in log order."""
    return [
        record.event
        for record in event_log(root).list_events()
        if record.event.event_type == REQUIREMENT_OUTCOME_UPDATED_EVENT_TYPE
        and record.event.object_id == requirement_id
    ]


def test_requirement_closure_persists_outcome_and_audits_event(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    result = close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=event_log(root),
    )
    assert result.replayed is False
    record = result.event_record
    assert record is not None and record.replayed is False
    # The persisted record is rewritten with the terminal outcome.
    stored = json.loads(
        (root / REQUIREMENTS_STATE_DIR / "REQ-1.json").read_text(encoding="utf-8")
    )
    assert stored["outcome"] == "REPRODUCED"
    assert read_requirement(root, "REQ-1").outcome is RequirementOutcome.REPRODUCED
    assert result.requirement.outcome is RequirementOutcome.REPRODUCED
    # The enforced classification is returned and auditable.
    assert result.assessment.state is RequirementClosureState.REPRODUCED
    assert result.assessment.matched_rule_id == "R-REQOUT-1"
    # One deterministic closure event under the audit event type.
    event = record.event
    assert event.event_type == REQUIREMENT_OUTCOME_UPDATED_EVENT_TYPE
    assert event.object_id == "REQ-1"
    assert event.from_ == "OPEN"
    assert event.to == "REPRODUCED"
    assert event.actor == ACTOR
    assert event.timestamp == AT
    assert event.reason == REASON
    assert event.payload == {
        "outcome": "REPRODUCED",
        "requirement_rule_id": "R-REQOUT-1",
    }
    assert event.event_id == generate_id(
        "event",
        REQUIREMENT_OUTCOME_UPDATED_EVENT_TYPE,
        "REQ-1",
        "OPEN",
        "REPRODUCED",
    )
    assert event_log(root).get(event.event_id) is not None
    assert len(closure_events(root)) == 1
    # The event resolves to the "Requirement outcome updated" git audit
    # checkpoint (the Supervisor flow owns the commit, never this API).
    mapping = map_event_to_audit(event)
    assert isinstance(mapping, AuditCommitRequest)
    assert mapping.kind == "requirement.outcome.updated"
    assert mapping.message == "requirement REQ-1 outcome updated"


@pytest.mark.parametrize("outcome,rule_id,state", TERMINAL_OUTCOMES)
def test_requirement_closure_accepts_every_terminal_outcome(
    tmp_path, outcome, rule_id, state
):
    root = init_project(tmp_path / outcome.value)
    registered_requirement(root)
    result = close_requirement(
        root,
        "REQ-1",
        outcome,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=event_log(root),
    )
    assert result.assessment.matched_rule_id == rule_id
    assert result.assessment.state is state
    assert read_requirement(root, "REQ-1").outcome is outcome
    record = result.event_record
    assert record is not None
    assert record.event.to == outcome.value
    assert record.event.payload["requirement_rule_id"] == rule_id


def test_requirement_closure_rejects_open_before_writing(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    path = root / REQUIREMENTS_STATE_DIR / "REQ-1.json"
    before = path.read_bytes()
    with pytest.raises(RequirementClosureError, match="cannot be closed"):
        close_requirement(
            root,
            "REQ-1",
            RequirementOutcome.OPEN,
            actor=ACTOR,
            at=AT,
            reason=REASON,
            event_log=event_log(root),
        )
    # Nothing was written and no event entered the audit record.
    assert path.read_bytes() == before
    assert closure_events(root) == []


def test_requirement_closure_unknown_and_unregistered_ids(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(RequirementNotFoundError):
        close_requirement(
            root,
            "REQ-1",
            RequirementOutcome.REPRODUCED,
            actor=ACTOR,
            at=AT,
            reason=REASON,
        )
    registered_requirement(root)
    with pytest.raises(RequirementNotFoundError):
        close_requirement(
            root,
            "REQ-MISSING",
            RequirementOutcome.REPRODUCED,
            actor=ACTOR,
            at=AT,
            reason=REASON,
        )
    with pytest.raises(InvalidRegistryIdError):
        close_requirement(
            root,
            "../escape",
            RequirementOutcome.REPRODUCED,
            actor=ACTOR,
            at=AT,
            reason=REASON,
        )


def test_requirement_closure_requires_initialized_project(tmp_path):
    with pytest.raises(ProjectNotInitializedError):
        close_requirement(
            tmp_path,
            "REQ-1",
            RequirementOutcome.REPRODUCED,
            actor=ACTOR,
            at=AT,
            reason=REASON,
        )


def test_requirement_closure_type_errors(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    with pytest.raises(TypeError):
        close_requirement(root, 1, RequirementOutcome.REPRODUCED, actor=ACTOR, at=AT, reason=REASON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        close_requirement(root, "REQ-1", "REPRODUCED", actor=ACTOR, at=AT, reason=REASON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        close_requirement(root, "REQ-1", RequirementOutcome.REPRODUCED, "HIGH", actor=ACTOR, at=AT, reason=REASON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        close_requirement(root, "REQ-1", RequirementOutcome.REPRODUCED, actor=1, at=AT, reason=REASON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        close_requirement(root, "REQ-1", RequirementOutcome.REPRODUCED, actor=ACTOR, at=1, reason=REASON)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        close_requirement(root, "REQ-1", RequirementOutcome.REPRODUCED, actor=ACTOR, at=AT, reason=1)  # type: ignore[arg-type]


def test_requirement_closure_rejects_empty_audit_args(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    with pytest.raises(RequirementClosureError, match="actor must not be empty"):
        close_requirement(root, "REQ-1", RequirementOutcome.REPRODUCED, actor="", at=AT, reason=REASON)
    with pytest.raises(RequirementClosureError, match="at must not be empty"):
        close_requirement(root, "REQ-1", RequirementOutcome.REPRODUCED, actor=ACTOR, at="", reason=REASON)
    with pytest.raises(RequirementClosureError, match="reason must not be empty"):
        close_requirement(root, "REQ-1", RequirementOutcome.REPRODUCED, actor=ACTOR, at=AT, reason="")


def test_requirement_closure_records_method_reproducibility(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    result = close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        method_reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=event_log(root),
    )
    assert read_requirement(root, "REQ-1").method_reproducibility is (
        MethodReproducibility.DIRECTLY_REPRODUCIBLE
    )
    assert result.requirement.method_reproducibility is (
        MethodReproducibility.DIRECTLY_REPRODUCIBLE
    )
    record = result.event_record
    assert record is not None
    assert record.event.payload["method_reproducibility"] == "DIRECTLY_REPRODUCIBLE"


def test_requirement_closure_undetermined_rating_is_legal(tmp_path):
    # The rating axis is schema-validated only (04-PROJECT-LIFECYCLE.md
    # section 6); UNDETERMINED never blocks a closure.
    root = init_project(tmp_path)
    registered_requirement(root)
    result = close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.INCONCLUSIVE,
        method_reproducibility=MethodReproducibility.UNDETERMINED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=event_log(root),
    )
    assert result.assessment.state is RequirementClosureState.INCONCLUSIVE
    record = result.event_record
    assert record is not None
    assert record.event.payload["method_reproducibility"] == "UNDETERMINED"


def test_requirement_closure_noop_guard_with_log(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    log = event_log(root)
    close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=log,
    )
    # The same closure is already fully recorded: rejected, and the
    # audit record must never get the event a second time.
    with pytest.raises(RequirementClosureError, match="already closed"):
        close_requirement(
            root,
            "REQ-1",
            RequirementOutcome.REPRODUCED,
            actor=ACTOR,
            at=AT,
            reason=REASON,
            event_log=log,
        )
    assert len(closure_events(root)) == 1


def test_requirement_closure_noop_guard_without_log(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
    )
    # Without an event log no convergence is possible: the no-op guard
    # always wins.
    with pytest.raises(RequirementClosureError, match="already closed"):
        close_requirement(
            root,
            "REQ-1",
            RequirementOutcome.REPRODUCED,
            actor=ACTOR,
            at=AT,
            reason=REASON,
        )


def test_requirement_closure_crash_window_heals_missing_event(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    # Interrupted first closure: the record write landed (persist-only
    # call), the event append never did.
    close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
    )
    assert closure_events(root) == []
    # Re-run with the log: the missing deterministic event is appended
    # (from OPEN) and the call converges as replayed.
    result = close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=event_log(root),
    )
    assert result.replayed is True
    (event,) = closure_events(root)
    assert event.from_ == "OPEN"
    assert event.to == "REPRODUCED"
    # Converged: a further re-run is a plain no-op rejection.
    with pytest.raises(RequirementClosureError, match="already closed"):
        close_requirement(
            root,
            "REQ-1",
            RequirementOutcome.REPRODUCED,
            actor=ACTOR,
            at=AT,
            reason=REASON,
            event_log=event_log(root),
        )
    assert len(closure_events(root)) == 1


def test_requirement_closure_rating_only_reclose_heals_from_last_event(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    log = event_log(root)
    close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=log,
    )
    # Rating-only re-closure without the log: the record is rewritten
    # with the rating, the event append is lost (crash window).
    close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        method_reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE,
        actor=ACTOR,
        at=AT,
        reason=REASON,
    )
    assert len(closure_events(root)) == 1
    # Heal: the last recorded event's 'to' is the from of the missing arc.
    result = close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        method_reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=log,
    )
    assert result.replayed is True
    (first, healed) = closure_events(root)
    assert (first.from_, first.to) == ("OPEN", "REPRODUCED")
    assert (healed.from_, healed.to) == ("REPRODUCED", "REPRODUCED")
    assert healed.payload["method_reproducibility"] == "DIRECTLY_REPRODUCIBLE"


def test_requirement_closure_reclose_chains_events(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    log = event_log(root)
    close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=log,
    )
    # Re-scoping between terminal outcomes is an update, not a duplicate:
    # the from/to arcs document the outcome line.
    result = close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.NOT_REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
        event_log=log,
    )
    assert result.replayed is False
    assert read_requirement(root, "REQ-1").outcome is RequirementOutcome.NOT_REPRODUCED
    first, second = closure_events(root)
    assert (first.from_, first.to) == ("OPEN", "REPRODUCED")
    assert (second.from_, second.to) == ("REPRODUCED", "NOT_REPRODUCED")


def test_requirement_closure_without_log_persists_only(tmp_path):
    root = init_project(tmp_path)
    registered_requirement(root)
    result = close_requirement(
        root,
        "REQ-1",
        RequirementOutcome.REPRODUCED,
        actor=ACTOR,
        at=AT,
        reason=REASON,
    )
    assert result.event_record is None
    assert read_requirement(root, "REQ-1").outcome is RequirementOutcome.REPRODUCED
    assert closure_events(root) == []
