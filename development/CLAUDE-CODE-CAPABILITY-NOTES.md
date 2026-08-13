# Claude Code Capability Notes

Verified against official Claude Code documentation on 2026-08-13. Treat these as platform facts for the development adapter, not timeless Core assumptions.

## Native `/goal`

- `/goal` keeps one session working across turns until a model evaluator judges a measurable condition satisfied.
- One goal can be active per session.
- Active goals are restored when the same session is resumed.
- `/goal` works non-interactively through `claude -p "/goal ..."`.
- The evaluator judges evidence already surfaced in the session; it does not independently run tests/read files. Therefore workers must run checks and surface results, and the Development Supervisor must independently re-run important checks.

## Agent Teams

- Agent Teams coordinate one lead and multiple independent Claude Code teammate sessions.
- Agent Teams are experimental and must currently be enabled explicitly.
- Tasks support dependencies and file-lock-based claiming.
- The lead can require teammate plan approval and can approve/reject plans autonomously.
- `TaskCompleted` and `TeammateIdle` hooks can block completion/idle and feed failures back to the teammate.
- Teammates have separate contexts and receive project context plus the spawn prompt, not the lead's full conversation history.
- In-process teammate sessions are not restored by lead `/resume`; after Supervisor resume, replacement teammates may be needed.
- No nested teams: only the lead manages teammates.
- Built-in commands entered while viewing an in-process teammate operate in the lead session. Therefore this specification does not pretend that the lead can reliably inject native `/goal` into Agent Team teammates.

## Development design consequence

Use:

1. native `/goal` on the Development Supervisor for the global M0–M13 terminal condition;
2. Agent Team task contracts + quality-gate hooks + Supervisor review for the default worker path;
3. detached `claude -p "/goal ..."` sessions when a particular worker benefits from native `/goal` semantics;
4. `.development/` as durable truth so Agent Team limitations cannot lose implementation progress.
