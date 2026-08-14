"""The named hook script of the quality gate (DEV-M10-G04).

The script the rendered hook configuration (:mod:`hooks.config`) names
for the ``TaskCompleted`` / ``TeammateIdle`` hook events. The real
Claude Code hook mechanism invokes it as a command: the event payload
JSON arrives on stdin and the decision JSON is written to stdout (a
``decision: block`` answer prevents the event -- the completion signal
is mechanically blocked).

Payload contract (documented for the deployment wiring)
-------------------------------------------------------
The real deployment resolves the durable task context from the shared
workspace and passes the hook payload as JSON with three fields:

* ``event`` -- the hook event (see :class:`HookEvent`): the event
  fields as Claude Code delivers them, plus the durable session
  identity the M10-G03 task bundle embeds;
* ``spec`` -- the durable verification context (see
  :class:`VerificationSpec`): the frozen goal contract's verification
  list of the gated task, read from the workspace records;
* ``registry_records`` -- the durable session registry snapshot
  (``SessionRegistry.to_records``), the shared-workspace reconstruction
  source of 13-EXECUTION-MONITOR.md SS4.

Decision contract
-----------------
The script answers the canonical decision dict
(:func:`decide_from_payload`): ``decision`` (``pass`` -- the completion
signal proceeds -- or ``block`` -- it is prevented) plus the full
:class:`GateRecord`. With no executing verifier the
:class:`UnavailableVerifier` refuses every check, so the gate blocks:
a verification that cannot be executed never completes a task
(AC-01: no fabricated completion). The real event delivery is NOT
exercised in the deterministic suite: the tests drive
:func:`decide_from_payload` -- the pure decision seam -- directly,
exactly like the M10-G03 adapter boundaries.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from scientific_reproduction.adapters.platform.base import PlatformAdapterDataError
from scientific_reproduction.adapters.platform.claude_code.hooks.gate import (
    UnavailableVerifier,
    VerificationSpec,
    Verifier,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.hook_events import (
    HookEvent,
    handle_hook_event,
)
from scientific_reproduction.adapters.platform.claude_code.session_registry import (
    SessionRegistry,
)

__all__ = [
    "decide_from_payload",
    "main",
]


def decide_from_payload(
    payload: Mapping[str, Any],
    *,
    verifier: Verifier | None = None,
) -> dict[str, Any]:
    """The pure hook decision of one payload: event + verification context.

    Turns the hook payload (the event fields, the durable verification
    context and the durable session registry snapshot) into the
    canonical decision dict a real hook runtime would emit:
    ``decision`` (``pass`` / ``block``) plus the full
    :class:`GateRecord`. With no ``verifier`` injected the
    :class:`UnavailableVerifier` refuses every check, so the gate
    blocks (AC-01: no fabricated completion). Deterministic: equal
    payloads produce byte-identical decisions; no wall clock, no
    randomness, no I/O.

    Raises:
        TypeError: ``payload`` is not a mapping.
        PlatformAdapterDataError: corrupt payload (missing event or
            spec fields, mismatched identities).
    """
    if not isinstance(payload, Mapping):
        raise TypeError(
            f"decide_from_payload expects a mapping, got {type(payload).__name__}"
        )
    missing = [name for name in ("event", "spec") if name not in payload]
    if missing:
        raise PlatformAdapterDataError(
            "corrupt hook payload: missing required field(s):"
            f" {', '.join(sorted(missing))}"
        )
    event = HookEvent.from_dict(payload["event"])
    spec = VerificationSpec.from_dict(payload["spec"])
    registry = SessionRegistry.from_records(payload.get("registry_records", ()))
    action = handle_hook_event(
        event,
        spec=spec,
        verifier=verifier if verifier is not None else UnavailableVerifier(),
        registry=registry,
    )
    return {"decision": action.decision.value, "gate": action.gate.to_dict()}


def main() -> int:
    """The real hook command entry (not exercised in the deterministic suite).

    Reads the event payload JSON from stdin, computes the decision and
    writes the decision JSON to stdout -- the Claude Code hook command
    contract. Real I/O, kept behind this boundary; the deterministic
    suite drives :func:`decide_from_payload` directly.
    """
    payload = json.load(sys.stdin)
    decision = decide_from_payload(payload)
    json.dump(decision, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
