# Contributing

Thank you for contributing to Scientific Reproduction Skill. Read the
normative documents before making any change:

- `docs/spec/index.md` — normative specification index (canonical paths)
- `20-ARCHITECTURE-DECISIONS.md` — locked architecture decisions
- `docs/adr/README.md` — how architecture decisions are recorded

## Integration model: one branch, one pull request

Every repository change is implemented on **one short-lived branch** and
integrated through **one pull request** targeting `main`.

### Rules that are not optional

1. **No direct commits to `main`.** All changes go through short-lived
   branches and pull requests.
2. **Authors never merge their own PR.** A PR is merged only after
   verification and CI pass and an independent reviewer approves it.
3. Force-pushing or rewriting `main` history is forbidden.

## Branch naming

Branch format: `<type-prefix>/<short-slug>`, e.g. `fix/cli-exit-codes`.
Prefixes: `feat/`, `fix/`, `refactor/`, `test/`, `docs/`, `ci/`, `build/`,
`chore/`.

## Commit messages

Use Conventional Commits:

```text
<type>(<scope>): <imperative summary>
```

Example:

```text
fix(cli): return exit code 2 for argument-parsing errors
```

Vague messages such as `update`, `changes`, or `fix stuff` are forbidden, as
are unrelated changes hidden inside a single commit.

## Pull requests

- Open the PR after the first push and fill in the PR template
  (`.github/PULL_REQUEST_TEMPLATE.md`): objective, scope changed,
  evidence, exact verification commands and results, architecture/contract
  impact, and known limitations.
- CI must pass before the PR can be reviewed for merge.

## Verification

Run the deterministic gates locally before opening the PR:

```text
python scripts/verify.py
```

This runs the pytest suite, the ruff lint gate, and the mypy type check —
the same gates CI runs on every PR. See `docs/user/installation.md` for
the manual equivalents.

## Merge authority

A PR is merged only after CI passes and an independent reviewer approves
(squash merge by default). A PR author does not self-approve, and the
implementation evidence never substitutes for reviewer approval.
