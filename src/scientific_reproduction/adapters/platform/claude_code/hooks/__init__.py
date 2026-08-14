"""Claude Code quality-gate hook templates (DEV-M10-G04).

The goal deliverable "quality gate hook templates" plus the "hook
configuration generator/documentation": deterministic quality-gate
hooks for Agent Teams task lifecycles (``TaskCompleted`` /
``TeammateIdle``). A task that signals completion (or a teammate that
goes idle) runs the frozen goal contract's verification list through
the injectable :class:`Verifier` boundary (:mod:`hooks.gate`); a
failing verification blocks the completion signal and feeds an
actionable, typed feedback record into the durable session outbox of
the M10-G03 :class:`SessionRegistry` semantics (:mod:`hooks.hook_events`
-- the wiring template the real hook mechanism would call). The gate
verdict is mechanical: it gates only the task-completion signal and
never replaces Supervisor review (AC-03). The hook configuration a real
deployment would install is rendered canonically by
:func:`render_hook_config` (:mod:`hooks.config`), naming
:mod:`hooks.entry`.

The real event delivery is NOT exercised: the deterministic test suite
drives the handler directly through the hermetically mockable boundary,
exactly like the M10-G03 adapter boundaries.
"""

from scientific_reproduction.adapters.platform.claude_code.hooks.config import (
    DEFAULT_HOOK_EVENTS,
    HOOK_MODULE_COMMAND,
    HookConfig,
    build_hook_config,
    render_hook_config,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.entry import (
    decide_from_payload,
    main,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.gate import (
    REQUIRED_FIX,
    VERIFIER_UNAVAILABLE_REPORT,
    FeedbackRecord,
    GateRecord,
    GateVerdict,
    UnavailableVerifier,
    VerificationOutcome,
    VerificationSpec,
    Verifier,
    evaluate_gate,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.hook_events import (
    HookAction,
    HookDecision,
    HookEvent,
    HookEventType,
    handle_hook_event,
)

__all__ = [
    # config
    "DEFAULT_HOOK_EVENTS",
    "HOOK_MODULE_COMMAND",
    "HookConfig",
    "build_hook_config",
    "render_hook_config",
    # entry
    "decide_from_payload",
    "main",
    # gate
    "REQUIRED_FIX",
    "VERIFIER_UNAVAILABLE_REPORT",
    "FeedbackRecord",
    "GateRecord",
    "GateVerdict",
    "UnavailableVerifier",
    "VerificationOutcome",
    "VerificationSpec",
    "Verifier",
    "evaluate_gate",
    # hook events
    "HookAction",
    "HookDecision",
    "HookEvent",
    "HookEventType",
    "handle_hook_event",
]
