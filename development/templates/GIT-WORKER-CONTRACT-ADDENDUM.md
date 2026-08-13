# Git Worker Contract Addendum

Every repository-changing Development Worker is bound by `25-DEVELOPMENT-GIT-GOVERNANCE.md`.

Required:

1. Work only in the assigned goal branch/worktree.
2. Do not push to `main`.
3. Keep changes inside the goal's allowed scope.
4. Use Conventional Commit messages containing the DEV-GOAL ID.
5. Run the frozen verification commands.
6. Push the goal branch and maintain the assigned Draft PR.
7. Report branch, commit SHAs, PR number/URL, head SHA, tests, and acceptance evidence.
8. Never merge or self-authorize PASS.
9. New functionality must become a `DEVELOPMENT_CHANGE_REQUEST`; do not implement it opportunistically.
10. Do not bypass hooks/checks or weaken acceptance/CI.
