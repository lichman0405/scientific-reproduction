# Contributing

Thank you for contributing to Scientific Reproduction Skill v0.1. Development of
this repository is governed by the frozen autonomous-development specification.
Read the normative documents before making any change:

- `25-DEVELOPMENT-GIT-GOVERNANCE.md` — branch, commit, PR, CI, and merge governance
- `development/git-policy.v1.yaml` — machine-readable git policy
- `21-DEVELOPMENT-SUPERVISOR-SPEC.md` — Development Supervisor authority and review gate
- `24-DEVELOPMENT-QUALITY-GATES.md` — quality gates

## Integration model: one branch, one pull request

Every repository-changing `DEV-GOAL` is implemented on **one short-lived branch**
and integrated through **one pull request** targeting `main`. After the one-time
empty-repository bootstrap commit, this is the **mandatory** integration path for
every repository-changing DEV-GOAL.

### Rules that are not optional

1. **No normal direct product commits to `main`.** A `DEV-GOAL` implementation
   must never be committed or pushed directly to `main`; the direct-to-`main`
   path exists only for the one-time specification-only bootstrap exception,
   which has already been applied.
2. **Workers never merge their own PR.** A Worker may create and update their
   goal branch, push it, and open a Draft PR, but may **not** merge the PR,
   approve it as final authority, or push to `main`.
3. **The Development Supervisor is the sole merge authority.** A PR may be
   merged only after its verification and required CI pass and the Supervisor
   independently reviews the head SHA and records PASS.
4. Force-pushing or rewriting `main` history is forbidden.

## Branch naming

Branch format: `<type-prefix>/<goal-id-lower>-<short-slug>`, e.g.
`ci/dev-m0-g00-github-governance`. Prefixes: `feat/`, `fix/`, `refactor/`,
`test/`, `docs/`, `ci/`, `build/`, `chore/`.

## Commit messages

Use Conventional Commits and identify the DEV-GOAL in every commit:

```text
<type>(<scope>): <imperative summary> [<DEV-GOAL-ID>]
```

Example:

```text
ci(repo): add pull-request quality gates [DEV-M0-G00]
```

Vague messages such as `update`, `changes`, or `fix stuff` are forbidden, as are
unrelated changes hidden inside a goal commit.

## Pull requests

- Open the PR as a **Draft** after the first push and fill in the PR template
  (`.github/PULL_REQUEST_TEMPLATE.md`): Goal ID and milestone, the frozen
  objective, scope changed, acceptance-criteria evidence, exact verification
  commands and results, architecture/contract impact, and known limitations.
- CI must pass before the PR can be reviewed for merge.

## Verification

Run the deterministic gates locally before opening the PR:

```text
python scripts/validate_development_contracts.py
```

## Merge authority

Only the Development Supervisor may authorize a merge (default method: squash).
Workers do not self-approve; the Worker result package never substitutes for
Supervisor review. See `25-DEVELOPMENT-GIT-GOVERNANCE.md` section 10-11 and
`development/git-policy.v1.yaml` for the full lifecycle and audit requirements.
