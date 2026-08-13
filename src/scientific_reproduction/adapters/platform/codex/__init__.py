"""Codex orchestration adapter (DEV-M10-G05).

The concrete :class:`PlatformAdapter` implementation for Codex:
persistent roles and goal-scoped workers run as Codex sessions through
the mockable session client (session transcripts persist locally as
JSONL rollout files), with the documented Codex resume limitation
reconciled explicitly (AC-03), the durable session identity kept in the
workspace-side registry rather than the session store (AC-02), and the
explicit one-shot headless exec fallback channel (``codex exec``, the
documented non-interactive mode with the machine-readable ``--json``
output) as the typed fallback path.

All Codex-specific interaction lives only inside this package and is
lazy/guarded, so the package imports and the adapter functions without a
live Codex CLI -- the test suite runs without a live Codex runtime and
fakes the interaction boundaries deterministically
(:class:`~scientific_reproduction.adapters.platform.codex.session_client.CodexSessionClient`,
:class:`~scientific_reproduction.adapters.platform.codex.process_runner.CodexProcessRunner`).
The adapter is orchestration transport only (AC-02): it imports no
scientific-core domain module -- only the platform-neutral seams the
DEV-M10-G02/G03 adapters legitimately share.
"""

from scientific_reproduction.adapters.platform.codex.adapter import (
    ALIVE_CLIENT_UNAVAILABLE_REASON,
    ALIVE_UNOBSERVABLE_REASON,
    CODEX_ADAPTER_VERSION,
    CODEX_PLATFORM_ID,
    COMMAND_OUTBOX_REASON,
    REPLACE_CLIENT_UNAVAILABLE_REASON,
    RESUME_CLIENT_UNAVAILABLE_REASON,
    RESUME_LIMITATION_REASON,
    RESUME_LIMITATION_RECORD_MISSING_REASON,
    SPAWN_CLIENT_UNAVAILABLE_REASON,
    SUBAGENT_FALLBACK_REASON,
    SUBAGENT_UNAVAILABLE_REASON,
    TERMINATE_CLIENT_UNAVAILABLE_REASON,
    TERMINATE_FALLBACK_REASON,
    TERMINATED_ALREADY_REASON,
    TERMINATED_COMMAND_REASON,
    TERMINATED_REPLACE_REASON,
    TERMINATED_RESUME_REASON,
    CodexPlatformAdapter,
)
from scientific_reproduction.adapters.platform.codex.process_runner import (
    CodexExecRunner,
    CodexProcessRunner,
    CodexRunResult,
    derive_run_id,
)
from scientific_reproduction.adapters.platform.codex.run_bundle import (
    build_run_bundle,
    render_exec_prompt,
)
from scientific_reproduction.adapters.platform.codex.session_client import (
    CodexSessionClient,
    CodexSessionClientStore,
    CodexSessionUnavailableError,
    SessionProbe,
    derive_session_id,
)
from scientific_reproduction.adapters.platform.codex.session_registry import (
    SessionRecord,
    SessionRegistry,
    SessionState,
)

__all__ = [
    # adapter
    "ALIVE_CLIENT_UNAVAILABLE_REASON",
    "ALIVE_UNOBSERVABLE_REASON",
    "CODEX_ADAPTER_VERSION",
    "CODEX_PLATFORM_ID",
    "CodexPlatformAdapter",
    "COMMAND_OUTBOX_REASON",
    "REPLACE_CLIENT_UNAVAILABLE_REASON",
    "RESUME_CLIENT_UNAVAILABLE_REASON",
    "RESUME_LIMITATION_RECORD_MISSING_REASON",
    "RESUME_LIMITATION_REASON",
    "SPAWN_CLIENT_UNAVAILABLE_REASON",
    "SUBAGENT_FALLBACK_REASON",
    "SUBAGENT_UNAVAILABLE_REASON",
    "TERMINATED_ALREADY_REASON",
    "TERMINATED_COMMAND_REASON",
    "TERMINATED_REPLACE_REASON",
    "TERMINATED_RESUME_REASON",
    "TERMINATE_CLIENT_UNAVAILABLE_REASON",
    "TERMINATE_FALLBACK_REASON",
    # process runner
    "CodexExecRunner",
    "CodexProcessRunner",
    "CodexRunResult",
    "derive_run_id",
    # run bundle
    "build_run_bundle",
    "render_exec_prompt",
    # session client
    "CodexSessionClient",
    "CodexSessionClientStore",
    "CodexSessionUnavailableError",
    "SessionProbe",
    "derive_session_id",
    # session registry
    "SessionRecord",
    "SessionRegistry",
    "SessionState",
]
