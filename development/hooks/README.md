# Claude Code development quality-gate hooks

The implementation Development Supervisor should configure Claude Code hooks when practical:

- `TaskCompleted`: run deterministic goal/milestone verification and exit 2 when the teammate attempts to complete a task whose required checks fail.
- `TeammateIdle`: reject idling when the assigned goal lacks a result package or mandatory checks.
- `TaskCreated`: optionally require task subjects to begin with `[DEV-Mx-Gyy]`.

Hooks are an early quality gate only. The Development Supervisor must independently inspect code and rerun acceptance checks before marking a canonical `.development` goal PASS.

Do not hardwire repository commands in this specification package. M0 establishes the implementation repository's canonical verify command; generated hooks should call that repository-owned verifier.
