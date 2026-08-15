"""Role agent definitions enforce authority at the platform tool level,
not only via prompt contracts (issue #88 -- defense-in-depth for ADR
26-29).

The Claude Code platform mechanism (``.claude/agents/*.md`` frontmatter
``tools:``) restricts a role session by tool name. Omitting the field
means "inherit every tool", which is exactly the gap this suite closes.
These tests pin the per-role allowlists to the locked spec:

* every role agent carries an explicit ``tools:`` allowlist of known
  platform tools;
* the Supervisor (``03-ROLE-AND-PERMISSION-SPEC.md`` SS2) holds the full
  grantable platform vocabulary -- the tool-level mirror of "Supervisor
  alone may ...";
* Research (SS3) holds read + runtime CLI + web source search (SS3 "may
  search public/open sources"), but no direct file-mutation tool (state
  writes limited to source/evidence through the runtime) and no
  worker-dispatch tool (SS3 "may not directly dispatch Workers");
* the Execution Monitor (SS4) holds read + runtime CLI + follow-up
  worker dispatch (SS4 "spawn follow-up collection/analysis workers"),
  but no direct file-mutation tool;
* the Worker (SS5-SS8) holds read + runtime CLI only: no
  plan/goal/acceptance writes -- no ``Write``/``Edit`` at all, because
  every state write flows through the runtime CLI, which enforces the
  role-action matrix -- and no dispatch, no web.

The suite is deterministic: it reads the four frontmatter files and
compares parsed YAML against the pinned vocabulary -- no wall clock, no
randomness.
"""

from __future__ import annotations

from pathlib import Path

import yaml

#: Repository root: tests/platform/ -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The role agent definition files, by agent name (the frontmatter
#: ``name:`` value; note the Execution Monitor agent name is hyphenated,
#: unlike the contract role_id "execution_monitor").
AGENT_DIR = REPO_ROOT / ".claude" / "agents"
ROLE_AGENTS: tuple[str, ...] = (
    "supervisor",
    "research",
    "execution-monitor",
    "worker",
)

#: Baseline every role holds (AC-02 truth sources + SKILL.md "Runtime
#: operations"): read project truth and operate the bundled runtime CLI.
#: State writes are runtime-mediated and matrix-enforced, never direct
#: file mutation.
READ_RUNTIME_TOOLS: frozenset[str] = frozenset({"Read", "Grep", "Glob", "Bash"})

#: Web source search (SS3 "search public/open sources").
WEB_SOURCE_SEARCH_TOOLS: frozenset[str] = frozenset({"WebFetch", "WebSearch"})

#: Session dispatch (SS2 "create Workers"; SS4 "spawn follow-up
#: collection/analysis workers"). ``Task`` is the legacy alias of
#: ``Agent`` on the platform; both are listed so the allowlist resolves
#: on current and older Claude Code versions.
DISPATCH_TOOLS: frozenset[str] = frozenset({"Agent", "Task"})

#: The full grantable platform tool vocabulary (Claude Code subagent
#: frontmatter ``tools:``). Pinned here so every allowlist entry
#: resolves to a real tool and any platform vocabulary growth is a
#: deliberate review point. MCP tools (``mcp__<server>__*``) are
#: deliberately not granted: the skill flow operates through the
#: bundled runtime, not MCP servers.
FULL_PLATFORM_VOCABULARY: frozenset[str] = frozenset(
    {
        "Agent",
        "Artifact",
        "Bash",
        "Edit",
        "EnterWorktree",
        "ExitWorktree",
        "Glob",
        "Grep",
        "Monitor",
        "NotebookEdit",
        "Read",
        "SendMessage",
        "Skill",
        "Task",
        "TaskStop",
        "TodoWrite",
        "ToolSearch",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)

#: The per-role tool allowlists, grounded in 03-ROLE-AND-PERMISSION-SPEC.md:
#: SS2 full vocabulary (Supervisor); SS3 read + runtime + web (Research);
#: SS4 read + runtime + follow-up dispatch (Monitor); SS5-SS8 read +
#: runtime only (Worker).
EXPECTED_TOOL_ALLOWLISTS: dict[str, frozenset[str]] = {
    "supervisor": FULL_PLATFORM_VOCABULARY,
    "research": READ_RUNTIME_TOOLS | WEB_SOURCE_SEARCH_TOOLS,
    "execution-monitor": READ_RUNTIME_TOOLS | DISPATCH_TOOLS,
    "worker": READ_RUNTIME_TOOLS,
}


def _parse_frontmatter(path: Path) -> dict[str, object]:
    """Parse the YAML frontmatter of a role agent definition file."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: missing opening frontmatter"
    _, body, _ = text.split("---", 2)
    data = yaml.safe_load(body) or {}
    assert isinstance(data, dict), f"{path}: frontmatter is not a mapping"
    return data


def _frontmatter_tools(role: str) -> frozenset[str]:
    """The parsed ``tools:`` allowlist of one role agent definition."""
    frontmatter = _parse_frontmatter(AGENT_DIR / f"{role}.md")
    tools = frontmatter.get("tools")
    assert isinstance(tools, list), (
        f"role agent {role!r} must define a tools allowlist"
    )
    assert all(isinstance(tool, str) and tool for tool in tools), (
        f"role agent {role!r}: every allowlist entry must be a non-empty"
        " tool name"
    )
    parsed = frozenset(tools)
    assert len(parsed) == len(tools), (
        f"role agent {role!r}: duplicate tool names in the allowlist"
    )
    return parsed


def test_agents_tools_every_role_defines_an_explicit_allowlist():
    # The gap the issue closes: omitting ``tools:`` means "inherit every
    # tool". Every role agent must therefore carry an explicit, non-empty
    # allowlist, and it must equal the spec-grounded set exactly.
    for role in ROLE_AGENTS:
        frontmatter = _parse_frontmatter(AGENT_DIR / f"{role}.md")
        assert frontmatter["name"] == role
        tools = frontmatter.get("tools")
        assert tools, f"role agent {role!r} must define a tools allowlist"
        assert _frontmatter_tools(role) == EXPECTED_TOOL_ALLOWLISTS[role], (
            f"role agent {role!r} tool allowlist drifted from"
            f" 03-ROLE-AND-PERMISSION-SPEC.md"
        )


def test_agents_tools_supervisor_holds_the_full_platform_vocabulary():
    # SS2: the Supervisor alone may create/freeze Plans, Goals, acceptance
    # criteria and protocols, decide transitions, dispatch workers and
    # assign final outcomes -- the tool-level mirror is the full grantable
    # platform vocabulary.
    assert _frontmatter_tools("supervisor") == FULL_PLATFORM_VOCABULARY
    for tool in FULL_PLATFORM_VOCABULARY:
        assert tool in _frontmatter_tools("supervisor")


def test_agents_tools_research_boundary_matches_spec_ss3():
    # SS3: read + runtime CLI + web source search -- the evidence-service
    # "may" list. No direct file mutation (source/evidence records are
    # written through the runtime) and no worker dispatch.
    assert _frontmatter_tools("research") == (
        READ_RUNTIME_TOOLS | WEB_SOURCE_SEARCH_TOOLS
    )
    assert "Write" not in _frontmatter_tools("research")
    assert "Edit" not in _frontmatter_tools("research")
    assert not (_frontmatter_tools("research") & DISPATCH_TOOLS)


def test_agents_tools_monitor_boundary_matches_spec_ss4():
    # SS4: read + runtime CLI + follow-up worker dispatch ("spawn
    # follow-up collection/analysis workers when the frozen workflow
    # requires it"). No direct file mutation.
    assert _frontmatter_tools("execution-monitor") == (
        READ_RUNTIME_TOOLS | DISPATCH_TOOLS
    )
    assert "Write" not in _frontmatter_tools("execution-monitor")
    assert "Edit" not in _frontmatter_tools("execution-monitor")


def test_agents_tools_worker_boundary_matches_spec_ss5_ss8():
    # SS5-SS8: read the frozen Goal Execution Context Package and operate
    # the runtime only -- no plan/goal/acceptance writes (no direct file
    # mutation at all), no dispatch, no web.
    assert _frontmatter_tools("worker") == READ_RUNTIME_TOOLS
    assert "Write" not in _frontmatter_tools("worker")
    assert "Edit" not in _frontmatter_tools("worker")
    assert not (_frontmatter_tools("worker") & DISPATCH_TOOLS)
    assert not (_frontmatter_tools("worker") & WEB_SOURCE_SEARCH_TOOLS)


def test_agents_tools_state_mutation_is_runtime_mediated_outside_supervisor():
    # Defense-in-depth: a Worker/Research/Monitor session holding
    # Write/Edit could mutate goals/plans/acceptance files directly,
    # bypassing the role-action matrix. Only the Supervisor holds direct
    # file-mutation tools; every other state write flows through the
    # runtime CLI, which enforces the matrix.
    for role in ("research", "execution-monitor", "worker"):
        tools = _frontmatter_tools(role)
        assert "Write" not in tools and "Edit" not in tools, (
            f"role {role!r} holds a direct file-mutation tool; state"
            " writes must be runtime-mediated"
        )
    assert {"Write", "Edit"} <= _frontmatter_tools("supervisor")


def test_agents_tools_dispatch_requires_spec_authority():
    # Session dispatch only where the spec grants it: SS2 "create
    # Workers" (Supervisor) and SS4 "spawn follow-up collection/analysis
    # workers" (Monitor). Research may not directly dispatch Workers
    # (SS3) and workers never dispatch.
    assert DISPATCH_TOOLS <= _frontmatter_tools("supervisor")
    assert DISPATCH_TOOLS <= _frontmatter_tools("execution-monitor")
    assert not (_frontmatter_tools("research") & DISPATCH_TOOLS)
    assert not (_frontmatter_tools("worker") & DISPATCH_TOOLS)


def test_agents_tools_web_access_is_source_search_only():
    # Web access only where the spec grants it: SS3 "search public/open
    # sources" (Research) and the Supervisor's full governance surface.
    # Workers execute the frozen context and the Monitor reconciles runs
    # -- neither searches sources.
    assert WEB_SOURCE_SEARCH_TOOLS <= _frontmatter_tools("research")
    assert not (_frontmatter_tools("worker") & WEB_SOURCE_SEARCH_TOOLS)
    assert not (_frontmatter_tools("execution-monitor") & WEB_SOURCE_SEARCH_TOOLS)


def test_agents_tools_allowlists_stay_inside_the_pinned_vocabulary():
    # Every allowlist entry resolves to a known grantable platform tool:
    # a misspelled or unknown tool name would silently fail to grant
    # anything on the platform, so the vocabulary is pinned here.
    for role in ROLE_AGENTS:
        assert _frontmatter_tools(role) <= FULL_PLATFORM_VOCABULARY, (
            f"role agent {role!r} names a tool outside the pinned"
            " platform vocabulary"
        )
