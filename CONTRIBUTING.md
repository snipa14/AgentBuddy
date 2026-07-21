# Contributing to Foundry

Foundry is currently a single-operator project without a hosted git remote
or open PR queue — the workflow below is how work actually gets done today,
written up so it's ready the moment that changes.

## Dev setup

No Python or Node install is required on the host; everything runs in
containers.

```bash
git clone <this-repo-url>
cd foundry
./scripts/start.sh   # or: ./foundry up
```

See the [Quickstart](README.md#quickstart) in the README for the secrets
wizard and first-run flow.

## Running tests and lint

```bash
# lint
ruff check . && ruff format --check .

# test a single service
pytest services/<name> --cov=services/<name> --cov-report=term-missing

# test everything, with the enforced coverage floor
pytest --cov=services --cov-fail-under=80 --cov-report=term-missing
```

Unit tests (`services/<name>/tests/unit`) are pure Python — no real Docker,
network, or filesystem beyond `tmp_path`. Integration tests
(`services/<name>/tests/integration`, marked `@pytest.mark.integration`)
need whatever real I/O that service actually requires — a live Docker
daemon for the Executor's sandbox-boundary tests in particular. Full
details in `.claude/rules/testing.md`.

`release/v1` must always pass both `pytest` and `ruff`. A change that
breaks either does not land there.

## Branching and commits

`release/v1` is this repo's active integration branch today — there is no
`main` yet, and no hosted remote or PR queue. In practice, day-to-day
`TODO.md` phase work has landed as a sequence of commits directly on
`release/v1` rather than one feature branch per phase; the branch-naming
scheme below is what a change *outside* that phase-by-phase flow (or any
future external contribution) should use:

```
feature/<short-description>       new capability
fix/<short-description>           bug fix
phase/<N>-<short-description>     work corresponding to a TODO.md phase
chore/<short-description>         tooling, deps, docs-only
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary in present tense, max 72 chars>

<optional body: why, not what>
```

Types: `feat`, `fix`, `test`, `docs`, `refactor`, `chore`, `ci`. Scope is
the service or layer affected (`executor`, `orchestrator`, `gateway`,
`memory`, `router`, ...). Full conventions in `.claude/rules/git.md`.

## Design discipline

- `TODO.md` is worked one phase at a time; later-phase work doesn't get
  pulled forward to unblock an earlier phase.
- A design decision not already covered by `ARCHITECTURE.md` §10 gets
  discussed and recorded there — as a new Decision Log entry — before the
  code changes, not after.
- No inline comments beyond narrow, non-obvious workarounds; public
  functions and classes get full docstrings (Google style). See the "Code
  conventions" section of `CLAUDE.md` for the complete style guide.

## Security boundary

The Executor is the only service allowed to talk to the Docker control
surface. If a change seems to need Docker access anywhere else, that's a
sign the design needs to change, not the boundary — see
[`SECURITY.md`](SECURITY.md) and `.claude/rules/security.md`.

## Secrets

Never commit a credential, token, or key — not even a fake-looking one in
a test. Tests that need one read it from an environment variable or the
Docker secrets path `/run/secrets/<name>`. `secrets/` is gitignored; only
`*.example` placeholders are committed.
