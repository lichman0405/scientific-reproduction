"""Runtime agent role contracts subsystem package (DEV-M10-G01).

Reusable, platform-neutral descriptors of the four runtime agent roles --
Supervisor, Research, Execution Monitor and Worker -- with the authority
boundaries of the locked role specification (``03-ROLE-AND-PERMISSION-SPEC.md``
SS2-SS8), Core state as the only truth source (AC-02) and the Worker's
plan-mutation / self-acceptance prohibitions (AC-03). Pure records:
frozen, typed, canonically serializable; no runtime execution code.
"""

from scientific_reproduction.adapters.platform.contracts.base import (
    CONTRACT_ROLE_IDS,
    CORE_STATE_API_PATHS,
    DECISION_ACTIONS,
    ROLE_CONTRACTS,
    ROLE_CONTRACTS_VERSION,
    STATE_TRUTH_RULE,
    VERDICT_ACTIONS,
    CoreStateApi,
    DecisionAuthority,
    RetryAuthority,
    RoleContract,
    RoleContractError,
    UnknownRoleContractError,
    VerdictAuthority,
    contract_to_matrix_roles,
    get_role_contract,
    validate_role_contracts,
)

__all__ = [
    "CONTRACT_ROLE_IDS",
    "CORE_STATE_API_PATHS",
    "DECISION_ACTIONS",
    "CoreStateApi",
    "DecisionAuthority",
    "RetryAuthority",
    "RoleContract",
    "RoleContractError",
    "ROLE_CONTRACTS",
    "ROLE_CONTRACTS_VERSION",
    "STATE_TRUTH_RULE",
    "UnknownRoleContractError",
    "VERDICT_ACTIONS",
    "VerdictAuthority",
    "contract_to_matrix_roles",
    "get_role_contract",
    "validate_role_contracts",
]
