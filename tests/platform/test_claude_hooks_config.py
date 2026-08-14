"""Claude Code quality-gate hook templates -- configuration generator (DEV-M10-G04).

The hook configuration generator renders the canonical JSON a real
deployment would install for the ``TaskCompleted`` / ``TeammateIdle``
hook events, naming the hook script (:mod:`hooks.entry`), from the
adapter's platform/capability context. The generator is pure and
canonical: equal inputs produce byte-identical output, and the payload
decision seam (:func:`decide_from_payload`) is a pure function of the
durable task context -- a verification that cannot be executed blocks,
never passes (AC-01: no fabricated completion).

The suite is pure: scripted in-memory fakes at the boundary, no wall
clock, no randomness, no file I/O -- the durable registry snapshot is a
plain records tuple replayed into a fresh registry.
"""

from __future__ import annotations

import json

import pytest

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
    PlatformCapability,
    PlatformOperation,
    SessionNotFoundError,
    WorkerSessionHandle,
)
from scientific_reproduction.adapters.platform.claude_code import (
    CLAUDE_CODE_ADAPTER_VERSION,
    CLAUDE_CODE_PLATFORM_ID,
    ClaudeCodePlatformAdapter,
)
from scientific_reproduction.adapters.platform.claude_code.hooks import (
    DEFAULT_HOOK_EVENTS,
    HOOK_MODULE_COMMAND,
    HookConfig,
    build_hook_config,
    decide_from_payload,
    render_hook_config,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.gate import (
    VERIFIER_UNAVAILABLE_REPORT,
    GateVerdict,
    VerificationOutcome,
    VerificationSpec,
    Verifier,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.hook_events import (
    HookEvent,
    HookEventType,
)
from scientific_reproduction.adapters.platform.claude_code.session_registry import (
    SessionRegistry,
)
from scientific_reproduction.core.ids import generate_id

PROJECT_ID = generate_id("project", "g04")
GOAL_ID = generate_id("goal", PROJECT_ID, "g1")
GOAL_VERIFICATION = ("python -m pytest -q tests/platform -k hook",)


def make_adapter() -> ClaudeCodePlatformAdapter:
    return ClaudeCodePlatformAdapter()


def make_handle() -> WorkerSessionHandle:
    return WorkerSessionHandle(
        platform_id=CLAUDE_CODE_PLATFORM_ID,
        worker_id="experiment_worker",
        role_id="worker",
        project_id=PROJECT_ID,
        goal_id=GOAL_ID,
        context_id=generate_id("worker-context", PROJECT_ID, GOAL_ID, "r1"),
    )


def make_registry_records() -> tuple[dict[str, object], ...]:
    registry = SessionRegistry()
    registry.put(make_handle())
    return registry.to_records()


class ScriptedVerifier(Verifier):
    """The deterministic in-suite verification fake of the config suite."""

    def __init__(self, results: dict[str, bool] | None = None) -> None:
        self.results = dict(results or {})
        self.checks: list[str] = []

    def check(self, command: str) -> VerificationOutcome:
        self.checks.append(command)
        passed = self.results.get(command, True)
        report = "verification passed" if passed else "verification failed"
        return VerificationOutcome(command=command, passed=passed, report=report)


def make_payload() -> dict[str, object]:
    """A canonical hook payload: event + durable verification context +
    the durable session registry snapshot."""
    handle = make_handle()
    event = HookEvent(
        event_type=HookEventType.TASK_COMPLETED,
        task_id=generate_id("team_task", handle.session_ref),
        session_ref=handle.session_ref,
        goal_id=GOAL_ID,
    )
    spec = VerificationSpec(
        session_ref=handle.session_ref,
        goal_id=GOAL_ID,
        commands=GOAL_VERIFICATION,
    )
    return {
        "event": event.to_dict(),
        "spec": spec.to_dict(),
        "registry_records": make_registry_records(),
    }


# ---------------------------------------------------------------------------
# The configuration generator (pure, canonical)
# ---------------------------------------------------------------------------


def test_hook_config_generation_is_canonical_and_byte_identical():
    # The generator is pure and canonical: equal inputs produce
    # byte-identical output, through both the renderer and the record.
    first = render_hook_config()
    second = render_hook_config()
    assert first == second
    assert render_hook_config() == build_hook_config().to_settings_json()

    capability = make_adapter().capabilities()
    assert render_hook_config(capability) == render_hook_config(capability)
    # the capability context and the frozen adapter constants carry the
    # same platform id and version, so the rendered fragments agree
    assert render_hook_config(capability) == render_hook_config()

    custom = render_hook_config(command="my-hook-script")
    assert custom == render_hook_config(command="my-hook-script")
    assert custom != first


def test_hook_config_names_the_entry_script_for_both_events():
    # The rendered configuration installs the quality gate for both
    # Agent Teams lifecycle events, each naming the hook script (the
    # hooks.entry module command).
    parsed = json.loads(render_hook_config())
    assert set(parsed["hooks"]) == {"TaskCompleted", "TeammateIdle"}
    for event in ("TaskCompleted", "TeammateIdle"):
        entries = parsed["hooks"][event]
        assert len(entries) == 1
        commands = [hook["command"] for hook in entries[0]["hooks"]]
        assert commands == [HOOK_MODULE_COMMAND]
        assert entries[0]["hooks"][0]["type"] == "command"
        assert HOOK_MODULE_COMMAND.endswith("hooks.entry")


def test_hook_config_carries_the_adapter_capability_context():
    # The generator renders the configuration from the adapter's
    # platform/capability context: the claude_code platform id and the
    # capability version, from an explicit capability or the frozen
    # adapter constants; a foreign platform capability is refused.
    capability = make_adapter().capabilities()
    parsed = json.loads(render_hook_config(capability))
    assert parsed["platform_id"] == CLAUDE_CODE_PLATFORM_ID
    assert parsed["version"] == CLAUDE_CODE_ADAPTER_VERSION

    default = json.loads(render_hook_config())
    assert default["platform_id"] == CLAUDE_CODE_PLATFORM_ID
    assert default["version"] == CLAUDE_CODE_ADAPTER_VERSION

    foreign = PlatformCapability(
        platform_id="codex",
        version="1.0",
        description="another platform",
        operations=(PlatformOperation.SPAWN_WORKER,),
    )
    with pytest.raises(PlatformAdapterDataError):
        render_hook_config(foreign)
    with pytest.raises(TypeError):
        render_hook_config("not a capability")  # type: ignore[arg-type]


def test_hook_config_record_round_trips_losslessly():
    # HookConfig is a frozen typed record whose serialization round-trips
    # losslessly; corrupt configuration state is a stable
    # PlatformAdapterDataError.
    config = build_hook_config(
        make_adapter().capabilities(), command="my-hook-script"
    )
    assert HookConfig.from_dict(config.to_dict()) == config
    assert config.platform_id == CLAUDE_CODE_PLATFORM_ID
    assert config.version == CLAUDE_CODE_ADAPTER_VERSION
    assert config.command == "my-hook-script"
    assert config.events == DEFAULT_HOOK_EVENTS
    with pytest.raises(PlatformAdapterDataError):
        HookConfig.from_dict({"platform_id": CLAUDE_CODE_PLATFORM_ID})
    with pytest.raises(PlatformAdapterDataError):
        HookConfig(
            platform_id="bad id!",  # type: ignore[arg-type]
            version="1.0",
            command=HOOK_MODULE_COMMAND,
            events=DEFAULT_HOOK_EVENTS,
        )
    with pytest.raises(PlatformAdapterDataError):
        HookConfig(
            platform_id=CLAUDE_CODE_PLATFORM_ID,
            version="not-a-version",
            command=HOOK_MODULE_COMMAND,
            events=DEFAULT_HOOK_EVENTS,
        )
    with pytest.raises(PlatformAdapterDataError):
        HookConfig(
            platform_id=CLAUDE_CODE_PLATFORM_ID,
            version="1.0",
            command="",
            events=DEFAULT_HOOK_EVENTS,
        )
    with pytest.raises(PlatformAdapterDataError):
        HookConfig(
            platform_id=CLAUDE_CODE_PLATFORM_ID,
            version="1.0",
            command=HOOK_MODULE_COMMAND,
            events=(),
        )
    with pytest.raises(PlatformAdapterDataError):
        build_hook_config(command=" ")


def test_hook_config_events_are_fixed_and_ordered():
    # The default events are the two Agent Teams lifecycle events in
    # fixed order (canonical rendering).
    assert DEFAULT_HOOK_EVENTS == (
        HookEventType.TASK_COMPLETED,
        HookEventType.TEAMMATE_IDLE,
    )
    parsed = json.loads(render_hook_config())
    assert list(parsed["hooks"]) == ["TaskCompleted", "TeammateIdle"]

    single = build_hook_config(events=(HookEventType.TASK_COMPLETED,))
    parsed_single = json.loads(single.to_settings_json())
    assert set(parsed_single["hooks"]) == {"TaskCompleted"}


# ---------------------------------------------------------------------------
# The payload decision seam (entry, pure)
# ---------------------------------------------------------------------------


def test_hook_payload_decision_is_pure_and_deterministic():
    # decide_from_payload turns the hook payload into the canonical
    # decision dict; equal payloads produce byte-identical decisions.
    payload = make_payload()
    passing = ScriptedVerifier({GOAL_VERIFICATION[0]: True})
    decision = decide_from_payload(payload, verifier=passing)
    assert decision == decide_from_payload(payload, verifier=passing)
    assert set(decision) == {"decision", "gate"}
    assert decision["decision"] == "pass"
    assert decision["gate"]["verdict"] == GateVerdict.PASS.value
    assert decision["gate"]["outcomes"][0]["passed"] is True


def test_hook_payload_blocks_without_an_executing_verifier():
    # Without an executing verifier the payload seam refuses every check
    # and blocks: a verification that cannot be executed never completes
    # a task (AC-01: no fabricated completion).
    payload = make_payload()
    decision = decide_from_payload(payload)
    assert decision["decision"] == "block"
    gate = decision["gate"]
    assert gate["verdict"] == GateVerdict.BLOCK.value
    assert gate["feedback"]["verifier_report"] == VERIFIER_UNAVAILABLE_REPORT
    assert gate["feedback"]["failed_command"] == GOAL_VERIFICATION[0]

    failing = decide_from_payload(
        payload, verifier=ScriptedVerifier({GOAL_VERIFICATION[0]: False})
    )
    assert failing["decision"] == "block"
    assert failing["gate"]["feedback"]["failed_command"] == GOAL_VERIFICATION[0]


def test_hook_payload_corrupt_data_is_a_stable_error():
    # A corrupt hook payload (missing fields, unknown session) is a
    # stable typed error -- never a silent decision.
    payload = make_payload()
    with pytest.raises(PlatformAdapterDataError):
        decide_from_payload({"event": payload["event"]})
    with pytest.raises(PlatformAdapterDataError):
        decide_from_payload({"event": {}, "spec": {}})
    with pytest.raises(TypeError):
        decide_from_payload("not a payload")  # type: ignore[arg-type]
    # the durable registry snapshot is the reconstruction source: a
    # payload without the session record is a broken reference (the
    # DEV-M10-G02 data-error discipline)
    unknown = dict(payload)
    unknown["registry_records"] = ()
    with pytest.raises(SessionNotFoundError):
        decide_from_payload(unknown)
