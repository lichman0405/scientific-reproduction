"""The run mapping of the Codex adapter (DEV-M10-G05).

The goal deliverable "mock/contract tests" plus "no scientific-core
fork": how one spawn maps onto the Codex session/exec machinery. The
adapter translates the durable session identity (the canonical
:class:`WorkerSessionHandle`) plus the frozen role contract
(``RoleContract``, DEV-M10-G01) into the canonical **run bundle** -- the
payload the Codex session client starts and the prompt the one-shot exec
fallback executes:

* the durable identity fields (``session_ref`` first, exactly the value
  the Core stores in ``Run.worker_session_ref``);
* the role contract's ``contract_id``/``contract_version`` (so the
  executed contract is auditable);
* the AC-02 truth rule (``state_truth_rule``: Core state is the only
  truth source, never conversation memory);
* the contract's ``prompt_obligations`` and ``prompt_prohibitions``
  (the "may"/"may not" directives the agent must follow).

The Core never embeds platform-specific command syntax
(15-ADAPTER-SPEC.md SS5): the bundle is platform-neutral and every
Codex-specific rendering (the session bundle, the ``codex exec`` prompt)
happens inside this package. The bundle carries **no scientific logic**:
the role contract is a platform-neutral descriptor imported from the
shared contracts package, exactly as the DEV-M10-G03 adapter does --
this module only maps identity + contract directives into the transport
payload (AC-02).

Determinism: canonical, sorted-free fixed field order, byte-identical
for equal inputs; no wall clock, no randomness, no I/O.
"""

from __future__ import annotations

from typing import Any

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
    WorkerSessionHandle,
)
from scientific_reproduction.adapters.platform.contracts.base import (
    RoleContract,
)

__all__ = [
    "build_run_bundle",
    "render_exec_prompt",
]

#: The bundle field order (canonical, stable).
_BUNDLE_FIELDS: tuple[str, ...] = (
    "session_ref",
    "platform_id",
    "worker_id",
    "role_id",
    "project_id",
    "goal_id",
    "context_id",
    "contract_id",
    "contract_version",
    "state_truth_rule",
    "prompt_obligations",
    "prompt_prohibitions",
)


def build_run_bundle(
    handle: WorkerSessionHandle, contract: RoleContract
) -> dict[str, Any]:
    """The canonical run bundle of one spawned session (AC-01 mapping).

    The payload the Codex session client starts and the one-shot exec
    fallback executes: the durable identity plus the frozen role
    contract directives. Deterministic and canonical -- equal inputs
    produce byte-identical bundles.

    Raises:
        TypeError: ``handle`` is not a :class:`WorkerSessionHandle` or
            ``contract`` is not a :class:`RoleContract`.
    """
    if not isinstance(handle, WorkerSessionHandle):
        raise TypeError(
            f"build_run_bundle expects a WorkerSessionHandle, got"
            f" {type(handle).__name__}"
        )
    if not isinstance(contract, RoleContract):
        raise TypeError(
            f"build_run_bundle expects a RoleContract, got"
            f" {type(contract).__name__}"
        )
    bundle: dict[str, Any] = dict(handle.to_dict())
    bundle["contract_id"] = contract.contract_id
    bundle["contract_version"] = contract.contract_version
    bundle["state_truth_rule"] = contract.state_truth_rule
    bundle["prompt_obligations"] = list(contract.prompt_obligations)
    bundle["prompt_prohibitions"] = list(contract.prompt_prohibitions)
    return bundle


def render_exec_prompt(bundle: dict[str, Any]) -> str:
    """Render the canonical run bundle into the one-shot exec prompt text.

    Deterministic, canonical, newline-joined: the durable identity plus
    the frozen contract directives, so a replacement one-shot run takes
    over with the same identity and the same contract without
    chat-memory access (13-EXECUTION-MONITOR.md SS4).

    Raises:
        TypeError: ``bundle`` is not a mapping.
        PlatformAdapterDataError: the bundle is missing a required field
            or carries a malformed field value (corrupt data).
    """
    if not isinstance(bundle, dict):
        raise TypeError(
            f"render_exec_prompt expects a bundle dict, got"
            f" {type(bundle).__name__}"
        )
    missing = [name for name in _BUNDLE_FIELDS if name not in bundle]
    if missing:
        raise PlatformAdapterDataError(
            "corrupt run bundle: missing required field(s):"
            f" {', '.join(missing)}"
        )
    lines: list[str] = []
    for key in _BUNDLE_FIELDS:
        value = bundle[key]
        if isinstance(value, list):
            if not all(isinstance(item, str) and item.strip() for item in value):
                raise PlatformAdapterDataError(
                    f"corrupt run bundle: {key!r} entries must be non-empty"
                    " strings"
                )
            lines.extend(f"- {item}" for item in value)
        elif isinstance(value, str):
            if not value.strip() and key not in ("goal_id", "context_id"):
                raise PlatformAdapterDataError(
                    f"corrupt run bundle: {key!r} must be a non-empty string"
                )
            lines.append(value)
        else:
            raise PlatformAdapterDataError(
                f"corrupt run bundle: {key!r} must be a str or list of str,"
                f" got {type(value).__name__}"
            )
    return "\n".join(lines)
