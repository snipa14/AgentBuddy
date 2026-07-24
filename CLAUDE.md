# Foundry

A self-hosted multi-agent software development platform. Agents plan, build, review, and test code autonomously inside isolated Docker containers, supervised through Telegram, with LangGraph orchestration and configurable local (Ollama) or remote (Claude API) inference.

## Current status

This repo is pre-Phase-0: only planning docs (`ARCHITECTURE.md`, `TODO.md`, this file) and `.claude/` config exist. There is no `services/` tree, no `scripts/`, no `pyproject.toml`, no `docker-compose.yml`, and no git history yet. Every checkbox in `TODO.md` is unchecked. The Commands and Architecture map sections below describe the target end-state (what Phase 0+ builds toward) — before assuming a file or command exists, check whether the relevant `TODO.md` phase is actually done.

## Key references

- Full architecture decisions and diagrams: `ARCHITECTURE.md`
- Phased execution plan: `TODO.md`
- Security threat model: `services/executor/SECURITY.md`

## Tech stack

- Python 3.12, `pyproject.toml`-based packaging
- LangGraph (StateGraph, interrupt/resume, SqliteSaver checkpointer)
- FastAPI (Orchestrator HTTP surface)
- aiogram (Telegram Gateway)
- Docker SDK for Python (Executor only — no other service)
- Pydantic v2 for all schemas and config validation
- pytest + pytest-cov for testing
- ruff for linting and formatting

## Commands

```bash
# start the stack (prompts for secrets on first run)
./scripts/start.sh

# start with local LLM (Ollama on RTX 3090)
./scripts/start.sh --local-llm

# stop
./scripts/stop.sh

# lint
ruff check . && ruff format --check .

# test (single service)
pytest services/<name> --cov=services/<name> --cov-report=term-missing

# test (all)
pytest --cov=services --cov-report=term-missing

# build workspace base images (run before first project)
docker build -t foundry/workspace-python:3.12 images/workspace-python/
docker build -t foundry/workspace-node:20 images/workspace-node/
docker build -t foundry/workspace-go:1.22 images/workspace-go/

# regenerate workspace Dockerfiles from config/stacks.yaml (run after editing stacks.yaml)
python3 scripts/gen_workspace_images.py
```

## Architecture map

```
services/
  telegram_gateway/   stateless bot, auth only, no business logic
  orchestrator/
    graph/            LangGraph StateGraph, nodes, edges, checkpointer
    agents/           one module per agent role (planner, coder, reviewer, tester, ...)
    memory/           SQLite/FTS5 global store + per-project markdown writers
    model_router/     capability-based provider resolver, no hardcoded model names
  executor/           ONLY service allowed to call Docker SDK; exposes typed tools
skills/               portable SKILL.md files, one per agent capability
projects/             bind-mounted project workspaces + per-project memory/ folders
config/models.yaml    model routing config — swap local ↔ Claude API here, not in code
```

## Code conventions

**No inline comments.** Code should be self-documenting. Name things so a comment would be redundant. If a function needs a paragraph of explanation, that explanation belongs in the module docstring or in `docs/`, not inline. Single-line `#` comments are permitted only for non-obvious workarounds with an associated GitHub issue reference.

**Comprehensive docstrings on every public function and class** — what it does, what it returns, what it raises. Google style docstrings.

**Naming:**
- `snake_case` for functions, variables, module names
- `PascalCase` for classes
- `SCREAMING_SNAKE_CASE` for module-level constants
- Agent node functions prefixed with `node_` (e.g., `node_planner`, `node_coder`)
- Executor tool functions prefixed with `tool_` (e.g., `tool_run_command`, `tool_write_file`)

**No bare `except`.** Always catch a specific exception type or `Exception` with a log.

**Pydantic for all external data.** Config files, API payloads, graph state dicts — all validated through Pydantic models before use.

**Type hints everywhere.** No `Any` without a comment explaining why.

## Phase discipline

Work one TODO.md phase at a time. Do not implement features from a later phase to unblock a current one — either finish the current phase or ask. At the end of each phase: run the full test suite, commit with message `Phase N: <description>`, update `ARCHITECTURE.md` if any design decision changed.

## Secrets

Never hardcode any credential, token, or key — not even in tests. Tests that need a credential use `pytest` fixtures that read from environment variables or the Docker secrets path `/run/secrets/<name>`. The `secrets/` directory is gitignored; only `*.example` placeholder files are committed.

## Model routing

Never reference a model name string (`claude-sonnet-4-6`, `qwen3-coder:30b-a3b`) directly in agent code. All model calls go through `model_router.resolve(role)`. Changing which model handles a role is a `config/models.yaml` edit, not a code change.

## Architecture decisions

Before making any design choice not already covered by `ARCHITECTURE.md` §10: stop, state the options, ask. Do not silently expand scope. If a decision is made mid-session, append it to the Decision Log in `ARCHITECTURE.md` before the session ends.

## Scoped rules (loaded on demand)

- Security boundary and Docker rules: `.claude/rules/security.md`
- Testing and coverage requirements: `.claude/rules/testing.md`
- Git workflow and branch discipline: `.claude/rules/git.md`

## Automated enforcement

Several of the rules above are backed by `PreToolUse` hooks in `.claude/settings.json` (see `.claude/hooks/`), not just convention — a violation blocks the tool call outright rather than relying on compliance:

- Bash commands touching `/var/run/docker.sock` or calling `docker` outside `compose`/`build`/`images`/`ps`/`logs` are blocked (`block_host_docker.py`).
- `git push`/`merge` targeting `main`, and any force push, are blocked (`block_main_push.py`) — a fallback for the graph's `interrupt()` merge gate.
- Host-level package installs (`pip install`, `apt install`, `npm install -g`, etc.) outside a `docker exec`/`docker run` context are blocked (`block_host_installs.py`).
- File writes matching known credential patterns (API keys, tokens, private key blocks) are blocked (`block_secret_writes.py`).
- File writes outside `services/executor/` that import the Docker SDK are blocked (`guard_executor_imports.py`).

If a task genuinely requires one of these (e.g. a legitimate Docker SDK import while building the Executor itself), the fix is to put the code in the right place (`services/executor/`), not to work around the hook.
