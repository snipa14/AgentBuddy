---
description: Git workflow, branch naming, commit discipline, and merge protection rules.
---

# Git Discipline

## Branch naming

```
feature/<short-description>       new capability
fix/<short-description>           bug fix
phase/<N>-<short-description>     work corresponding to a TODO.md phase (preferred for phase work)
chore/<short-description>         tooling, deps, docs-only
```

All work happens on a branch. Never commit directly to `main`.

## Commit messages

Conventional Commits format:

```
<type>(<scope>): <short summary in present tense, max 72 chars>

<optional body: why, not what — the diff already shows what>
```

Types: `feat`, `fix`, `test`, `docs`, `refactor`, `chore`, `ci`
Scope: the service or layer affected (e.g., `executor`, `orchestrator`, `gateway`, `memory`, `router`)

Phase-end commits use: `feat(phase-N): <description of what was built>`

## What never goes in a commit

- Any value that looks like a key, token, password, or secret — even a fake-looking one. The pre-commit `gitleaks` hook will block it, but don't rely on that.
- Binary files that belong in volumes (model weights, SQLite databases, `.db` files).
- Generated files that are reproducible from source (`__pycache__`, `.coverage`, `dist/`, `*.egg-info/`).
- `projects/*/` workspace content — this directory is gitignored except `projects/*/memory/*.md`.

## main is always green

`main` must always pass `pytest` and `ruff`. If a commit would break `main`, it does not go to `main`. Use the branch. Open a PR (or use the Git/GitHub Integrator agent with `pr_mode: auto`) only when tests are green.

## Merge to main gate — enforce, do not bypass

Merging to `main` requires an explicit human approval via Telegram, always. This is not negotiable even if:
- All tests are green
- The PR has been open for a week
- The change is a typo fix

In the graph, this is the `interrupt(payload=merge_request)` node in the Git/GitHub Integrator subgraph. Do not short-circuit it, do not add a flag to skip it, do not mock it out in a "quick test". The test for this gate explicitly asserts that no merge call reaches the Docker/GitHub API without a resumed approval (see `tests/integration/test_merge_gate.py`).

## Squash preference

Prefer squash-merging feature branches to keep `main` history readable. Each squashed commit on `main` should represent one complete, tested, working capability — not a series of "WIP" states.

## After a phase commit

Tag phase-end commits: `git tag phase-N-done` (lightweight, not annotated). This makes it easy to `git diff phase-N-done..phase-M-done` to see exactly what a phase produced.
