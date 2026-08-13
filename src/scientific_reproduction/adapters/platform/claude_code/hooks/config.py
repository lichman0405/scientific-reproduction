"""Hook configuration generator and mapping documentation (DEV-M10-G04).

The goal deliverable "hook configuration generator/documentation": the
canonical JSON hook configuration a real Claude Code deployment would
install for the quality gate, rendered from the adapter's
platform/capability context. The generator is pure and canonical:
equal inputs produce byte-identical output.

Claude Code hook mechanism (mapping documentation)
--------------------------------------------------
Claude Code runs hook commands configured in ``settings.json`` under a
``hooks`` map keyed by hook event name; a hook command receives the
event JSON on stdin and must emit a decision JSON on stdout. A
``decision: block`` answer (with a ``reason``) prevents the event from
proceeding. For Agent Teams task lifecycles the task-completion and
idle events -- ``TaskCompleted`` / ``TeammateIdle`` -- are the natural
placement of the deterministic quality gate (DEV-M10-G04): the named
hook script (:mod:`hooks.entry`) resolves the durable task context
(the event payload carries the task record and the embedded durable
session identity; the durable verification context is reconstructed
from the shared workspace), runs the deterministic verification step
and answers ``pass`` (the completion signal proceeds) or ``block``
(the completion is prevented and the feedback is recorded into the
durable session outbox).

The rendered configuration is a fragment: the top-level
``platform_id`` / ``version`` record the adapter capability context the
fragment was generated for, and the ``hooks`` map is the installable
settings entry. A real deployment merges the ``hooks`` map into the
project's ``.claude/settings.json`` (or the user settings). The real
event delivery is NOT exercised in the deterministic test suite: the
rendered configuration and the hook boundary are pinned by tests, and
the suite drives the handler directly.

Determinism: pure descriptor layer -- frozen records, ``TypeError`` at
the public boundaries, stable one-line error messages, canonical
sorted serialization with lossless round-trip, and no wall clock, no
randomness, no I/O.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
    PlatformCapability,
)
from scientific_reproduction.adapters.platform.claude_code.adapter import (
    CLAUDE_CODE_ADAPTER_VERSION,
    CLAUDE_CODE_PLATFORM_ID,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.hook_events import (
    HookEventType,
)

#: The hook script the rendered configuration names by default: the
#: ``hooks.entry`` module invoked as a Python module command (the
#: deployment's ``python`` launcher; deterministic across machines).
HOOK_MODULE_COMMAND: str = (
    "python -m scientific_reproduction.adapters.platform.claude_code.hooks.entry"
)

#: The hook events the default configuration installs (fixed order).
DEFAULT_HOOK_EVENTS: tuple[HookEventType, ...] = (
    HookEventType.TASK_COMPLETED,
    HookEventType.TEAMMATE_IDLE,
)

#: The hook entry shape of the settings hooks map (stable).
_HOOK_COMMAND_KIND = "command"

#: Valid platform backend id shape (same contract as the interface).
_PLATFORM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: Valid version shape (``major.minor``, same contract as the interface).
_VERSION_PATTERN = re.compile(r"^\d+\.\d+$")

__all__ = [
    "DEFAULT_HOOK_EVENTS",
    "HOOK_MODULE_COMMAND",
    "HookConfig",
    "build_hook_config",
    "render_hook_config",
]


@dataclass(frozen=True)
class HookConfig:
    """One canonical hook configuration record of the quality gate.

    ``platform_id`` -- the platform backend the configuration targets
    (the claude_code platform id); ``version`` -- the adapter capability
    version the configuration was generated for; ``command`` -- the hook
    script command the hook events name; ``events`` -- the hook events
    installed, in fixed order.
    """

    platform_id: str
    version: str
    command: str
    events: tuple[HookEventType, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.platform_id, str):
            raise TypeError(
                "HookConfig.platform_id must be a str, got"
                f" {type(self.platform_id).__name__}"
            )
        if not _PLATFORM_ID_PATTERN.fullmatch(self.platform_id):
            raise PlatformAdapterDataError(
                "HookConfig.platform_id must match ^[a-z][a-z0-9_]*$, got"
                f" {self.platform_id!r}"
            )
        if not isinstance(self.version, str):
            raise TypeError(
                "HookConfig.version must be a str, got"
                f" {type(self.version).__name__}"
            )
        if not _VERSION_PATTERN.fullmatch(self.version):
            raise PlatformAdapterDataError(
                f"HookConfig.version must match ^\\d+\\.\\d+$, got"
                f" {self.version!r}"
            )
        if not isinstance(self.command, str) or not self.command.strip():
            raise PlatformAdapterDataError(
                "HookConfig.command must be a non-empty string, got"
                f" {self.command!r}"
            )
        if not isinstance(self.events, tuple):
            raise TypeError(
                "HookConfig.events must be a tuple of HookEventType, got"
                f" {type(self.events).__name__}"
            )
        for event in self.events:
            if not isinstance(event, HookEventType):
                raise TypeError(
                    "HookConfig.events entries must be HookEventType members,"
                    f" got {type(event).__name__}"
                )
        if not self.events:
            raise PlatformAdapterDataError(
                "HookConfig.events must not be empty: at least one hook"
                " event is required"
            )

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the config in canonical field order."""
        return {
            "platform_id": self.platform_id,
            "version": self.version,
            "command": self.command,
            "events": [event.value for event in self.events],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HookConfig:
        """Build a config from a plain dict (corrupt state is a stable
        PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "HookConfig.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name
            for name in ("platform_id", "version", "command", "events")
            if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt hook config: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                platform_id=data["platform_id"],
                version=data["version"],
                command=data["command"],
                events=tuple(HookEventType(value) for value in data["events"]),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt hook config: {exc}"
            ) from exc

    def to_settings_json(self) -> str:
        """The canonical installable settings hooks fragment (JSON).

        Byte-identical for equal configs: the ``hooks`` map keyed by the
        installed hook events, each naming the hook script command, plus
        the adapter capability context header. A real deployment merges
        the ``hooks`` map into ``.claude/settings.json``.
        """
        hooks: dict[str, Any] = {}
        for event in self.events:
            hooks[event.value] = [
                {
                    "hooks": [
                        {"type": _HOOK_COMMAND_KIND, "command": self.command},
                    ]
                }
            ]
        return json.dumps(
            {
                "platform_id": self.platform_id,
                "version": self.version,
                "hooks": hooks,
            },
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )


def build_hook_config(
    capability: PlatformCapability | None = None,
    *,
    command: str | None = None,
    events: tuple[HookEventType, ...] = DEFAULT_HOOK_EVENTS,
) -> HookConfig:
    """Build the canonical hook configuration record of the quality gate.

    ``capability`` -- the adapter's capability context (the claude_code
    platform); when given, its platform id must be the claude_code
    platform and its version becomes the configuration's version. When
    None the frozen claude_code adapter constants are used. ``command``
    -- the hook script command; None names the default
    :data:`HOOK_MODULE_COMMAND` (the ``hooks.entry`` module). ``events``
    -- the hook events to install, default both Agent Teams lifecycle
    events in fixed order.

    Raises:
        TypeError: ``capability`` is not a :class:`PlatformCapability`.
        PlatformAdapterDataError: ``capability`` is a capability of a
            different platform (this generator is claude_code-specific),
            or ``command`` is not a non-empty string.
    """
    if capability is not None and not isinstance(capability, PlatformCapability):
        raise TypeError(
            f"capability must be a PlatformCapability, got"
            f" {type(capability).__name__}"
        )
    if capability is not None and capability.platform_id != CLAUDE_CODE_PLATFORM_ID:
        raise PlatformAdapterDataError(
            "the claude_code hook configuration generator cannot render a"
            f" capability of platform {capability.platform_id!r}"
        )
    if command is not None:
        if not isinstance(command, str):
            raise TypeError(
                f"command must be a str, got {type(command).__name__}"
            )
        if not command.strip():
            raise PlatformAdapterDataError(
                "command must be a non-empty string, got"
                f" {command!r}"
            )
        resolved_command = command
    else:
        resolved_command = HOOK_MODULE_COMMAND
    return HookConfig(
        platform_id=CLAUDE_CODE_PLATFORM_ID,
        version=(
            capability.version
            if capability is not None
            else CLAUDE_CODE_ADAPTER_VERSION
        ),
        command=resolved_command,
        events=events,
    )


def render_hook_config(
    capability: PlatformCapability | None = None,
    *,
    command: str | None = None,
    events: tuple[HookEventType, ...] = DEFAULT_HOOK_EVENTS,
) -> str:
    """Render the canonical JSON hook configuration of the quality gate.

    The installable settings hooks fragment for the given capability
    context and hook events (see :func:`build_hook_config` and
    :meth:`HookConfig.to_settings_json`). Pure and canonical: equal
    inputs produce byte-identical output.
    """
    return build_hook_config(capability, command=command, events=events).to_settings_json()
