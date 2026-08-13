"""Role contract hygiene: frozen records, deterministic ids, canonical
serialization, no wall clock or randomness (DEV-M10-G01).

The contract layer is a pure descriptor layer. These tests pin the house
conventions of the adapter modules:

* records are frozen dataclasses that validate their own contract
  (``__post_init__`` structural checks; ``validate_role_contracts``
  matrix-consistency checks);
* ids are deterministic pure functions of canonical fields
  (``core.ids.generate_id("role_contract", role_id, version)`` --
  ``sr_role_contract_<32 hex>``), unique across roles;
* serialization is canonical and deterministic (sorted keys, sorted set
  members, byte-identical for equal records, lossless round-trip);
* the layer contains no wall clock and no randomness (no
  timestamp-like fields, no random/uuid/time/datetime imports, repeated
  evaluation is byte-identical);
* public boundaries raise ``TypeError`` for wrong types and
  ``ValueError``-subclassed errors with stable one-line messages.

The suite is pure: no file I/O, no wall clock, no randomness.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from dataclasses import FrozenInstanceError

import pytest

from scientific_reproduction.adapters.platform.contracts import (
    CONTRACT_ROLE_IDS,
    ROLE_CONTRACTS,
    ROLE_CONTRACTS_VERSION,
    RoleContract,
    RoleContractError,
    UnknownRoleContractError,
    contract_to_matrix_roles,
    get_role_contract,
    validate_role_contracts,
)
from scientific_reproduction.adapters.platform.contracts.base import (
    CoreStateApi,
    DecisionAuthority,
    RetryAuthority,
    VerdictAuthority,
)
from scientific_reproduction.core.ids import ID_PATTERN, generate_id, is_valid_id
from scientific_reproduction.core.permissions import Action, Role


def test_contracts_hygiene_records_are_frozen_dataclasses_that_validate():
    # Frozen records in the house style: every contract is a frozen
    # dataclass, and mutating any field raises FrozenInstanceError.
    assert len(ROLE_CONTRACTS) == 4
    for contract in ROLE_CONTRACTS:
        assert dataclasses.is_dataclass(contract)
        assert isinstance(contract, RoleContract)
        with pytest.raises(FrozenInstanceError):
            contract.role_name = "mutated"
        with pytest.raises(FrozenInstanceError):
            contract.allowed_actions = frozenset()
    # Set members are immutable frozensets / tuples.
    for contract in ROLE_CONTRACTS:
        assert isinstance(contract.allowed_actions, frozenset)
        assert isinstance(contract.forbidden_actions, frozenset)
        assert isinstance(contract.forbidden_practices, frozenset)
        assert isinstance(contract.truth_sources, frozenset)
        assert isinstance(contract.state_object_types, frozenset)
        assert isinstance(contract.prompt_obligations, tuple)
        assert isinstance(contract.prompt_prohibitions, tuple)


def test_contracts_hygiene_registry_is_the_frozen_four_role_table():
    # The registry is the frozen four-role table in normative order
    # (SS2-SS8: supervisor, research, monitor, worker), with unique
    # role ids and unique contract ids.
    role_ids = tuple(contract.role_id for contract in ROLE_CONTRACTS)
    assert role_ids == CONTRACT_ROLE_IDS
    assert len(set(role_ids)) == 4
    contract_ids = tuple(contract.contract_id for contract in ROLE_CONTRACTS)
    assert len(set(contract_ids)) == 4
    assert all(isinstance(contract.role_id, str) for contract in ROLE_CONTRACTS)


def test_contracts_hygiene_contract_ids_are_deterministic_safe_ids():
    # Deterministic ids in the house style: sr_role_contract_<32 hex>,
    # a pure function of (role_id, version) -- same inputs, same id;
    # distinct roles, distinct ids.
    for contract in ROLE_CONTRACTS:
        assert ID_PATTERN.fullmatch(contract.contract_id)
        assert is_valid_id(contract.contract_id, kind="role_contract")
        expected = generate_id(
            "role_contract", contract.role_id, contract.contract_version
        )
        assert contract.contract_id == expected
        again = get_role_contract(contract.role_id)
        assert again.contract_id == contract.contract_id
    expected_ids = {
        generate_id("role_contract", role_id, ROLE_CONTRACTS_VERSION)
        for role_id in CONTRACT_ROLE_IDS
    }
    assert {contract.contract_id for contract in ROLE_CONTRACTS} == expected_ids


def test_contracts_hygiene_serialization_is_canonical_and_deterministic():
    # Canonical serialization: equal contracts produce byte-identical
    # dicts and JSON; the round-trip is lossless; set members are sorted.
    for contract in ROLE_CONTRACTS:
        assert contract.to_dict() == contract.to_dict()
        assert contract.to_json() == contract.to_json()
        assert contract.to_dict()["allowed_actions"] == sorted(
            contract.to_dict()["allowed_actions"]
        )
        assert contract.to_dict()["truth_sources"] == sorted(
            contract.to_dict()["truth_sources"]
        )
        restored = RoleContract.from_dict(contract.to_dict())
        assert restored == contract
        assert restored.to_json() == contract.to_json()
        parsed = json.loads(contract.to_json())
        assert parsed == contract.to_dict()


def test_contracts_hygiene_validate_role_contracts_passes_and_grounds_the_matrix():
    # The integrity validator passes on the frozen table and returns the
    # role ids in normative order; the matrix mapping is total and
    # grounded in the locked role vocabulary.
    assert validate_role_contracts() == CONTRACT_ROLE_IDS
    assert contract_to_matrix_roles("supervisor") == (Role.SUPERVISOR,)
    assert contract_to_matrix_roles("research") == (Role.RESEARCH,)
    assert contract_to_matrix_roles("execution_monitor") == (Role.MONITOR,)
    assert contract_to_matrix_roles("worker") == (
        Role.EXPERIMENT_WORKER,
        Role.COMPUTATION_WORKER,
        Role.ANALYSIS_WORKER,
        Role.DIAGNOSIS_WORKER,
    )


def test_contracts_hygiene_no_wall_clock_and_no_randomness_in_the_layer():
    # Determinism hygiene: no timestamp-like fields in the records and no
    # wall-clock/randomness imports in the contract module -- the layer
    # is a pure function of its frozen inputs.
    for contract in ROLE_CONTRACTS:
        keys = set(contract.to_dict())
        assert not any(key in keys for key in ("timestamp", "created_at", "updated_at", "dispatched_at"))
    source = inspect.getsource(RoleContract)
    module_source = inspect.getsource(contract_to_matrix_roles)
    for token in ("import random", "import uuid", "import time", "import datetime", "random.", "uuid."):
        assert token not in source
        assert token not in module_source


def test_contracts_hygiene_authority_and_state_fields_are_typed_enums():
    # Typed fields in the house style: authority members and state APIs
    # are enum members of the frozen vocabulary, never free strings.
    for contract in ROLE_CONTRACTS:
        assert isinstance(contract.decision_authority, DecisionAuthority)
        assert isinstance(contract.verdict_authority, VerdictAuthority)
        assert isinstance(contract.retry_authority, RetryAuthority)
        for source in contract.truth_sources:
            assert isinstance(source, CoreStateApi)
        for action in contract.allowed_actions | contract.forbidden_actions:
            assert isinstance(action, Action)


def test_contracts_hygiene_lookup_returns_the_same_frozen_record():
    # get_role_contract returns the canonical frozen record by role id.
    for contract in ROLE_CONTRACTS:
        assert get_role_contract(contract.role_id) is contract
    assert get_role_contract("worker").role_id == "worker"


def test_contracts_hygiene_unknown_role_lookup_raises_stable_error():
    # Unknown ids raise the ValueError-subclassed error with a stable,
    # one-line message; wrong types raise TypeError.
    with pytest.raises(UnknownRoleContractError) as exc:
        get_role_contract("scientist")
    message = str(exc.value)
    assert "scientist" in message
    assert "supervisor" in message
    assert "\n" not in message
    assert issubclass(UnknownRoleContractError, RoleContractError)
    assert issubclass(RoleContractError, ValueError)
    with pytest.raises(TypeError):
        get_role_contract(42)
    with pytest.raises(TypeError):
        get_role_contract(None)


def test_contracts_hygiene_constructor_rejects_wrong_types_and_values():
    # Public boundaries: TypeError for wrong types, RoleContractError for
    # invalid values, with stable messages.
    base = get_role_contract("worker")
    with pytest.raises(TypeError):
        RoleContract(
            role_id=42,
            role_name=base.role_name,
            spec_section=base.spec_section,
            mission=base.mission,
            contract_version=base.contract_version,
            allowed_actions=base.allowed_actions,
            forbidden_actions=base.forbidden_actions,
            forbidden_practices=base.forbidden_practices,
            truth_sources=base.truth_sources,
            state_object_types=base.state_object_types,
            decision_authority=base.decision_authority,
            verdict_authority=base.verdict_authority,
            retry_authority=base.retry_authority,
            state_truth_rule=base.state_truth_rule,
            prompt_obligations=base.prompt_obligations,
            prompt_prohibitions=base.prompt_prohibitions,
        )
    with pytest.raises(RoleContractError):
        RoleContract(
            role_id="not_a_contract_role",
            role_name=base.role_name,
            spec_section=base.spec_section,
            mission=base.mission,
            contract_version=base.contract_version,
            allowed_actions=base.allowed_actions,
            forbidden_actions=base.forbidden_actions,
            forbidden_practices=base.forbidden_practices,
            truth_sources=base.truth_sources,
            state_object_types=base.state_object_types,
            decision_authority=base.decision_authority,
            verdict_authority=base.verdict_authority,
            retry_authority=base.retry_authority,
            state_truth_rule=base.state_truth_rule,
            prompt_obligations=base.prompt_obligations,
            prompt_prohibitions=base.prompt_prohibitions,
        )
    with pytest.raises(TypeError):
        RoleContract(
            role_id=base.role_id,
            role_name=base.role_name,
            spec_section=base.spec_section,
            mission=base.mission,
            contract_version=base.contract_version,
            allowed_actions={"not", "a", "frozenset"},
            forbidden_actions=base.forbidden_actions,
            forbidden_practices=base.forbidden_practices,
            truth_sources=base.truth_sources,
            state_object_types=base.state_object_types,
            decision_authority=base.decision_authority,
            verdict_authority=base.verdict_authority,
            retry_authority=base.retry_authority,
            state_truth_rule=base.state_truth_rule,
            prompt_obligations=base.prompt_obligations,
            prompt_prohibitions=base.prompt_prohibitions,
        )
    with pytest.raises(TypeError):
        RoleContract.from_dict("not a mapping")
    with pytest.raises(RoleContractError) as exc:
        RoleContract.from_dict({})
    assert "missing required field" in str(exc.value)


def test_contracts_hygiene_validate_rejects_duplicate_and_inconsistent_tables():
    # The integrity validator detects duplicate role ids and matrix-
    # inconsistent authority grants in candidate tables (stable messages).
    with pytest.raises(RoleContractError) as exc:
        validate_role_contracts(ROLE_CONTRACTS + (ROLE_CONTRACTS[0],))
    assert "duplicate role id" in str(exc.value)
    with pytest.raises(RoleContractError) as exc:
        validate_role_contracts([])
    assert "must not be empty" in str(exc.value)
    with pytest.raises(TypeError):
        validate_role_contracts("not a sequence")
    # A candidate table that grants a matrix-denied action (Goal creation
    # to Research) is rejected by the AC-01 grounding check.
    research = get_role_contract("research")
    overgranted = RoleContract(
        role_id=research.role_id,
        role_name=research.role_name,
        spec_section=research.spec_section,
        mission=research.mission,
        contract_version=research.contract_version,
        allowed_actions=research.allowed_actions | frozenset({Action.GOAL_CREATE}),
        forbidden_actions=research.forbidden_actions
        - frozenset({Action.GOAL_CREATE}),
        forbidden_practices=research.forbidden_practices,
        truth_sources=research.truth_sources,
        state_object_types=research.state_object_types,
        decision_authority=research.decision_authority,
        verdict_authority=research.verdict_authority,
        retry_authority=research.retry_authority,
        state_truth_rule=research.state_truth_rule,
        prompt_obligations=research.prompt_obligations,
        prompt_prohibitions=research.prompt_prohibitions,
    )
    with pytest.raises(RoleContractError) as exc:
        validate_role_contracts((overgranted,))
    assert "locked role-action matrix denies" in str(exc.value)


def test_contracts_hygiene_deterministic_reproduction_across_evaluation():
    # Whole-table determinism: repeated evaluation of the registry
    # reproduces identical records and byte-identical serializations.
    def snapshot() -> tuple[str, ...]:
        return tuple(contract.to_json() for contract in ROLE_CONTRACTS)

    assert snapshot() == snapshot()
    assert len({contract.role_id for contract in ROLE_CONTRACTS}) == 4
