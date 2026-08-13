"""Role contracts reference Core state, never conversation memory
(DEV-M10-G01 AC-02).

Every contract must name the Core state APIs as its truth sources --
the state backend (``core.state_backend.StateBackend``: run records,
plan/goal/analysis objects, durable state) and the append-only project
event log (``core.events.ProjectEventLog``) -- and its ``state_object_types``
must be normative object types of the frozen model registry
(``core.models.SCHEMA_NAMES``). No contract field may name an LLM's
remembered conversation as a data source; the only place the words
"memory" / "remembered" appear is the AC-02 rule that negates them.

The suite is pure: no file I/O, no wall clock, no randomness.
"""

from __future__ import annotations

import importlib

from scientific_reproduction.adapters.platform.contracts import (
    CORE_STATE_API_PATHS,
    ROLE_CONTRACTS,
    CoreStateApi,
)
from scientific_reproduction.core.models import SCHEMA_NAMES

#: The two Core state APIs the contracts may name as truth sources --
#: resolved against the real modules so a rename breaks the test.
STATE_BACKEND_MODULE = importlib.import_module(
    "scientific_reproduction.core.state_backend"
)
EVENT_LOG_MODULE = importlib.import_module("scientific_reproduction.core.events")

#: Memory-reference tokens that must never appear outside the AC-02 rule.
MEMORY_TOKENS = (
    "conversation memory",
    "chat memory",
    "llm memory",
    "remembered context",
    "recall from conversation",
    "from memory",
)


def _text_fields(contract) -> list[str]:
    """The free-text fields of a contract (mission, directives, rule)."""
    return [
        contract.mission,
        contract.spec_section,
        contract.state_truth_rule,
        *contract.prompt_obligations,
        *contract.prompt_prohibitions,
    ]


def test_contracts_ac02_every_contract_names_core_state_api_truth_sources():
    # AC-02: every contract names both Core state APIs as truth sources,
    # and the API paths resolve to the real Core modules -- the contract
    # points at actual state machinery, not at remembered context.
    for contract in ROLE_CONTRACTS:
        assert contract.truth_sources, (
            f"role contract {contract.role_id!r} names no truth source"
        )
        assert contract.truth_sources <= frozenset(CoreStateApi)
        assert contract.truth_sources == frozenset(
            {CoreStateApi.STATE_BACKEND, CoreStateApi.EVENT_LOG}
        )
    assert CORE_STATE_API_PATHS[CoreStateApi.STATE_BACKEND] == (
        "scientific_reproduction.core.state_backend.StateBackend"
    )
    assert CORE_STATE_API_PATHS[CoreStateApi.EVENT_LOG] == (
        "scientific_reproduction.core.events.ProjectEventLog"
    )
    assert hasattr(STATE_BACKEND_MODULE, "StateBackend")
    assert hasattr(EVENT_LOG_MODULE, "ProjectEventLog")


def test_contracts_ac02_state_object_types_are_normative_core_types():
    # AC-02: every state object type a contract names is a normative type
    # of the frozen model registry (SCHEMA_NAMES) -- run records, event
    # log records and plan/goal objects are Core state, not prompt text.
    for contract in ROLE_CONTRACTS:
        assert contract.state_object_types
        for obj_type in contract.state_object_types:
            assert obj_type in SCHEMA_NAMES, (
                f"role contract {contract.role_id!r} names non-normative"
                f" object type {obj_type!r}"
            )
    by_id = {contract.role_id: contract for contract in ROLE_CONTRACTS}
    # The Monitor reads Run records and writes event records; workers read
    # the Goal Execution Context Package and register artifacts; the
    # Supervisor reads/writes the full project state.
    assert "run" in by_id["execution_monitor"].state_object_types
    assert "event" in by_id["execution_monitor"].state_object_types
    assert "worker-context" in by_id["worker"].state_object_types
    assert "artifact-manifest" in by_id["worker"].state_object_types
    assert "run" in by_id["supervisor"].state_object_types
    assert "event" in by_id["supervisor"].state_object_types
    assert "decision" in by_id["supervisor"].state_object_types


def test_contracts_ac02_serialized_contracts_contain_no_memory_reference():
    # AC-02: the canonical serialized contract carries no conversation-
    # memory reference in any free-text field except the AC-02 rule, which
    # exists to negate memory as a truth source.
    for contract in ROLE_CONTRACTS:
        serialized = contract.to_json().lower()
        for token in MEMORY_TOKENS:
            assert token not in serialized, (
                f"role contract {contract.role_id!r} serialization mentions"
                f" {token!r}, a conversation-memory truth source (AC-02)"
            )
        for field in _text_fields(contract):
            lowered = field.lower()
            for token in MEMORY_TOKENS:
                assert token not in lowered, (
                    f"role contract {contract.role_id!r} field references"
                    f" {token!r}"
                )


def test_contracts_ac02_truth_rule_names_core_state_and_negates_memory():
    # AC-02: the prompt-facing truth rule names the state backend and the
    # event log as the authoritative record and explicitly denies an
    # agent's remembered conversation context.
    for contract in ROLE_CONTRACTS:
        rule = contract.state_truth_rule.lower()
        assert "state backend" in rule
        assert "event log" in rule
        assert "never" in rule
        assert "memory" in rule
    assert all(
        contract.state_truth_rule == ROLE_CONTRACTS[0].state_truth_rule
        for contract in ROLE_CONTRACTS
    )


def test_contracts_ac02_prompt_directives_ground_decisions_in_state():
    # AC-02 at the prompt level: the directives tell the agent where truth
    # lives (state backend, event log, persisted records) -- never "as I
    # remember from our conversation".
    supervisor = ROLE_CONTRACTS[0]
    assert supervisor.role_id == "supervisor"
    directives = " ".join(supervisor.prompt_obligations).lower()
    assert "state" in directives
    assert "event log" in directives
    monitor = {c.role_id: c for c in ROLE_CONTRACTS}["execution_monitor"]
    monitor_directives = " ".join(monitor.prompt_obligations).lower()
    assert "persisted run records" in monitor_directives
    assert "event log" in monitor_directives
    for contract in ROLE_CONTRACTS:
        assert all(
            field.strip() for field in _text_fields(contract)
        )
