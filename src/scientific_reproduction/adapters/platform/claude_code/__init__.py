"""Claude Code orchestration adapter (DEV-M10-G03).

The concrete :class:`PlatformAdapter` implementation for Claude Code:
persistent roles and goal-scoped workers run as Agent Teams tasks
(``worker_mode: agent_team_task`` -- the currently available
orchestration mechanism), with the known Agent Teams resume limitation
reconciled explicitly (AC-03), the durable session identity kept in the
workspace-side registry rather than the task store (AC-02), and the
explicit subagent/process fallback channel (``claude -p`` headless
single-shot) as the typed fallback path.

All Claude-specific imports live only inside this package and are
dynamic/guarded, so the package imports and the adapter functions
without the Agent SDK -- the test suite runs without a live SDK and
fakes the interaction boundaries deterministically
(:class:`~scientific_reproduction.adapters.platform.claude_code.team_store.TeamStoreClient`,
:class:`~scientific_reproduction.adapters.platform.claude_code.process_runner.ProcessRunner`).
"""

from scientific_reproduction.adapters.platform.claude_code.adapter import (
    ALIVE_STORE_UNAVAILABLE_REASON,
    ALIVE_UNOBSERVABLE_REASON,
    CLAUDE_CODE_ADAPTER_VERSION,
    CLAUDE_CODE_PLATFORM_ID,
    COMMAND_OUTBOX_REASON,
    REPLACE_STORE_UNAVAILABLE_REASON,
    RESUME_LIMITATION_REASON,
    RESUME_LIMITATION_RECORD_MISSING_REASON,
    RESUME_STORE_UNAVAILABLE_REASON,
    SPAWN_STORE_UNAVAILABLE_REASON,
    SUBAGENT_FALLBACK_REASON,
    SUBAGENT_UNAVAILABLE_REASON,
    TERMINATE_FALLBACK_REASON,
    TERMINATE_STORE_UNAVAILABLE_REASON,
    TERMINATED_ALREADY_REASON,
    TERMINATED_COMMAND_REASON,
    TERMINATED_REPLACE_REASON,
    TERMINATED_RESUME_REASON,
    ClaudeCodePlatformAdapter,
)
from scientific_reproduction.adapters.platform.claude_code.process_runner import (
    ClaudeSubprocessRunner,
    ProcessRunner,
    SubprocessSpawn,
    derive_spawn_id,
)
from scientific_reproduction.adapters.platform.claude_code.session_registry import (
    SessionRecord,
    SessionRegistry,
    SessionState,
)
from scientific_reproduction.adapters.platform.claude_code.task_bundle import (
    build_task_bundle,
    render_subagent_prompt,
)
from scientific_reproduction.adapters.platform.claude_code.team_store import (
    AgentTeamTaskStore,
    TaskStoreProbe,
    TeamStoreClient,
    TeamStoreUnavailableError,
    derive_task_id,
)

__all__ = [
    # adapter
    "ALIVE_STORE_UNAVAILABLE_REASON",
    "ALIVE_UNOBSERVABLE_REASON",
    "CLAUDE_CODE_ADAPTER_VERSION",
    "CLAUDE_CODE_PLATFORM_ID",
    "ClaudeCodePlatformAdapter",
    "COMMAND_OUTBOX_REASON",
    "REPLACE_STORE_UNAVAILABLE_REASON",
    "RESUME_LIMITATION_RECORD_MISSING_REASON",
    "RESUME_LIMITATION_REASON",
    "RESUME_STORE_UNAVAILABLE_REASON",
    "SPAWN_STORE_UNAVAILABLE_REASON",
    "SUBAGENT_FALLBACK_REASON",
    "SUBAGENT_UNAVAILABLE_REASON",
    "TERMINATED_ALREADY_REASON",
    "TERMINATED_COMMAND_REASON",
    "TERMINATED_REPLACE_REASON",
    "TERMINATED_RESUME_REASON",
    "TERMINATE_FALLBACK_REASON",
    "TERMINATE_STORE_UNAVAILABLE_REASON",
    # process runner
    "ClaudeSubprocessRunner",
    "ProcessRunner",
    "SubprocessSpawn",
    "derive_spawn_id",
    # session registry
    "SessionRecord",
    "SessionRegistry",
    "SessionState",
    # task bundle
    "build_task_bundle",
    "render_subagent_prompt",
    # team store
    "AgentTeamTaskStore",
    "TeamStoreClient",
    "TeamStoreUnavailableError",
    "TaskStoreProbe",
    "derive_task_id",
]
