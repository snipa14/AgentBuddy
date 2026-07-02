# Foundry — Build TODO

This file is the execution plan for ARCHITECTURE.md. It is meant to be worked through with Claude Code, one phase at a time.

## How to Use This File

- Work phase by phase, top to bottom. Don't skip ahead — later phases assume earlier ones are done and tested.
- Each phase lists: **Goal**, **Decisions it depends on** (ADR numbers from ARCHITECTURE.md §10), **Tasks**, **Definition of Done**.
- Commit at the end of each phase with a message referencing the phase number (e.g. `git commit -m "Phase 3: executor + docker security boundary"`).
- Every phase that adds executable code must include tests. Do not move to the next phase with failing tests or coverage below the project's configured threshold.
- If a decision is needed that ARCHITECTURE.md §10 doesn't cover, stop and ask — don't guess and don't silently expand scope.
- If a phase reveals the design needs to change, update ARCHITECTURE.md first, then continue. The doc and the code should never drift apart.

---

## Phase 0 — Repository Bootstrap

**Goal:** empty but structured repo, ready for development tooling.
**Decisions:** ADR-012 (Python-first), ADR-014 (skills)

- [x] `git init`; `.gitignore` covering Python (`__pycache__`, `.venv`), Docker, `secrets/*` (except `*.example`), `*.env`, and project working dirs under `projects/*/` except their `memory/` subfolders
- [x] Create the directory skeleton from ARCHITECTURE.md §9
- [x] `pyproject.toml`: Python 3.12, `pytest`, `pytest-cov`, `ruff`, `mypy` (optional but recommended), `pydantic`
- [x] `./foundry` CLI wrapper script (bash or a thin Python entrypoint) with subcommands: `up`, `down`, `status`, `new-project`, `logs`
- [x] Pre-commit config: `ruff` + a secret scanner (`gitleaks` or `detect-secrets`) — this is the safety net against an agent ever committing a real key
- [x] `LICENSE` — MIT recommended for portfolio visibility; confirm before making the repo public
- [x] `README.md` stub: one-paragraph pitch, link to `ARCHITECTURE.md`, "Status: in development"

**Definition of Done:** `git log` shows an initial commit; `./foundry --help` prints subcommands; the pre-commit hook runs on `git commit`.

---

## Phase 1 — Secrets & Compose Skeleton

**Goal:** `docker compose config` resolves cleanly; the secrets wizard works end to end.
**Decisions:** ADR-004, ADR-005, ADR-010

- [x] `docker-compose.yml` with services: `telegram-gateway`, `orchestrator`, `executor`, `docker-socket-proxy` (placeholder image `python:3.12-slim` is fine until real Dockerfiles exist)
- [x] `secrets:` blocks for `telegram_bot_token`, `claude_api_key`, `github_token`, `telegram_allowed_user_id`
- [x] `secrets/*.example` placeholders committed; real files gitignored
- [x] `scripts/setup.sh`: interactive wizard — prompts for each missing secret, writes it to `secrets/`, sets file mode `600`
- [x] `scripts/start.sh` (runs `setup.sh` if any secret is missing, then `docker compose up -d`) and `scripts/stop.sh` (`docker compose down`)
- [x] `docker-compose.local-llm.yml` overlay: `ollama` service with GPU reservation for the RTX 3090 and a named volume for downloaded models
- [x] `docker-socket-proxy` configured per ARCHITECTURE.md §4.3 (`CONTAINERS=1 EXEC=1 IMAGES=1 POST=1`, everything else `0`), with the real host socket mounted into **this container only**

**Definition of Done:** fresh clone → `./scripts/start.sh` prompts for secrets once → `docker compose ps` shows all core services `Up`. Running it a second time does not re-prompt.

---

## Phase 2 — Model Router

**Goal:** a tested library that resolves agent role → provider, where swapping local ↔ Claude is a config change, not a code change, and an all-Claude deployment is a first-class, well-tested path — not just a theoretical config edit.
**Decisions:** ADR-001, ADR-015, ADR-016

- [x] `services/orchestrator/model_router/`: a `Provider` protocol (`complete()`, `supports(capability)`), plus `OllamaProvider` and `AnthropicProvider` implementations
- [x] `config/models.yaml` loader with Pydantic schema validation, supporting multiple named provider entries of the same `type` (e.g. `claude_opus`, `claude_sonnet`, `claude_haiku`), each independently referenceable in a role's `prefer` list (ADR-015)
- [x] `resolve(role: str) -> Provider`, implementing the `prefer` fallback chain
- [x] `local` provider availability is determined once at process startup from the active Compose profile (e.g. an `OLLAMA_ENABLED` env var), not via a per-call network probe/timeout to Ollama (ADR-015)
- [x] Ship `config/models.claude-only.yaml`: a documented preset with every role's `prefer` list containing only `claude_*` providers and no `local` entry
- [x] Unit tests: resolves to the first available preferred provider; falls back correctly when the preferred one is unreachable (mock the HTTP client); raises a clear, typed error if no configured provider satisfies the role's required capabilities; with the Claude-only preset loaded and `OLLAMA_ENABLED` unset, `resolve()` never constructs or calls `OllamaProvider` for any role
- [x] Short doc (README section or `docs/model-router.md`) on adding a new provider or role, and on the Claude-only preset

**Definition of Done:** `pytest services/orchestrator/model_router` is green at the project's configured coverage threshold; setting every role's `prefer` to a `claude_*` provider in `models.yaml` requires zero code changes; loading `config/models.claude-only.yaml` resolves every role to a Claude provider with zero attempted Ollama connections.

---

## Phase 3 — Executor & Docker Security Boundary

**Goal:** a narrow, tested tool API, and proof the security boundary actually holds.
**Decisions:** ADR-005

- [x] `images/workspace-python/Dockerfile`: non-root user, Python 3.12, common build deps, no unnecessary setuid binaries
- [x] `services/executor/`: implements `run_command`, `read_file`, `write_file`, `list_files`, `run_tests` against the Docker SDK for Python — talking only to `docker-socket-proxy`'s HTTP endpoint, never the raw socket
- [x] Per-call enforcement: project-scoped bind mount only, `cap_drop=["ALL"]`, `security_opt=["no-new-privileges"]`, explicit `mem_limit`/`nano_cpus`, isolated network (egress allowlist itself deferred — see SECURITY.md's accepted residual risk)
- [x] Lifecycle: create → exec → capture stdout/stderr/exit_code → remove, wrapped in try/finally so a timeout or crash never leaves an orphaned container
- [x] Integration test (real Docker): spawn a workspace container, `write_file` into it, confirm the change lands in the bind-mounted host directory, confirm a command inside the container cannot read any path outside the mount, confirm an attempt to reach `docker.sock` directly from inside the container fails
- [x] `services/executor/SECURITY.md`: explicit threat model — what's prevented, what's accepted residual risk

**Definition of Done:** the integration suite proves containment (every escape attempt fails); a `write_file` → host-visible round trip works against a live container.
**Verification status:** `pydantic`/`httpx`/`pyyaml`/`pytest`/`docker` are installed as of this session — `services/executor`'s unit suite is green for real (34 tests, 84% coverage; 95% across the whole repo). The real-Docker integration suite (`pytest -m integration services/executor/tests/integration`) is written and skips cleanly here (no Docker daemon in this sandbox) but has **not yet run against a live daemon** — that's the one thing still needed before this phase's Definition of Done is actually met. Run it on a Docker-enabled machine and report back.

---

## Phase 4 — Telegram Gateway

**Goal:** bot responds only to the allow-listed user, forwards to the Orchestrator, renders inline-keyboard approvals.
**Decisions:** ADR-002

- [x] `services/telegram_gateway/`: bot setup (aiogram), long-polling preferred over webhooks (simpler for an on-demand host without a stable public IP)
- [x] Auth middleware: silently drop (but log) any update from a user ID other than the configured `telegram_allowed_user_id`
- [x] Commands: `/start`, `/new`, `/status`, `/projects`
- [x] Free-text forwarding: message → `POST {ORCHESTRATOR_URL}/webhook`
- [x] Approval rendering: given a notification carrying `pending_approval`, render text + inline keyboard (`Approve` / `Reject` / `Edit`); button callback → `POST {ORCHESTRATOR_URL}/resume/{thread_id}`
- [x] Unit tests with a mocked Telegram client and a mocked Orchestrator HTTP client

**Definition of Done:** a message from an unauthorized user ID produces no bot response (but is logged); an authorized message reaches a stub Orchestrator endpoint in an integration test.
**Verification status:** `aiogram` is not yet installed in this sandbox, so none of this has been executed — code and both test suites are written but unverified. Install `aiogram` and run `pytest services/telegram_gateway -v` (unit) and `pytest -m integration services/telegram_gateway/tests/integration -v` (the stub-forwarding proof) before treating this phase as done.

---

## Phase 5 — Orchestrator Core (LangGraph Skeleton)

**Goal:** global graph + per-project subgraph compile and run end to end with stub nodes; interrupt/resume works and survives a restart.
**Decisions:** ADR-006, ADR-008, ADR-009

- [x] `ProjectState` (ARCHITECTURE.md §5) as a `TypedDict`
- [x] `SqliteSaver` checkpointer pointed at the `foundry_memory.db` volume
- [x] Project subgraph with placeholder nodes for each phase (`intake → planning → implementing → testing → done`), each logging and advancing for now
- [x] The two hard `interrupt()` points implemented as real graph interrupts: cost-threshold check (stub: trip it on a hardcoded test value) and merge-to-main (stub)
- [x] Global graph: routes `POST /webhook` payloads to the right project thread by `project_id` (or starts a new one) -- implemented as a plain Python routing layer (`services/orchestrator/service.py`) over the compiled subgraph rather than a second nested StateGraph; see that module's docstring for why. The persistent project registry is Phase 7.
- [x] FastAPI app: `/webhook`, `/resume/{thread_id}`, `/projects`, `/status/{project_id}`
- [x] Test: full round trip — start a project, hit the stubbed cost interrupt, resume via `/resume`, confirm state reflects the decision; kill and restart the process, confirm the pending interrupt is still there (proves checkpointer persistence, not just in-memory state) -- see `test_pending_interrupt_survives_a_simulated_restart` in `services/orchestrator/graph/tests/unit/test_graph_end_to_end.py`

**Definition of Done:** `pytest services/orchestrator/graph` is green; a manual `curl` against `/webhook` then `/resume` produces the expected phase transitions, including across a process restart.

**Verification status:** `langgraph`, `langgraph-checkpoint-sqlite`, and `langchain-core` are still not installed in this sandbox (`fastapi`/`uvicorn` were added via `apk` mid-Phase-6 and confirmed importable), so the graph/service/API code is still unverified -- `graph/tests/unit/`, `tests/unit/test_service.py`, and `tests/unit/test_api.py` all fail at collection with `ModuleNotFoundError: No module named 'langgraph'`. `tests/unit/test_config.py` *is* now verified (2 passed) after a Phase 6 fix -- see that phase's notes below for why `services/orchestrator/__init__.py` stopped eagerly importing `service.py`. Install `langgraph`, `langgraph-checkpoint-sqlite`, and `langchain-core`, then run `pytest services/orchestrator -v` before treating this phase as done. The riskiest untested assumptions, in order of likelihood of needing a fix once real: (1) `from langgraph.graph.state import CompiledStateGraph` as a type-hint import path, (2) `SqliteSaver(conn)` direct-construction signature and its `.conn` attribute name (used by the restart test to close the connection), (3) `BaseCheckpointSaver.list(None)` returning checkpoints across *all* threads rather than requiring a thread_id filter, (4) `graph.get_state(config)` returning `values={}` for a thread with no checkpoint history yet (rather than raising), used to detect "unknown project".

---

## Phase 6 — Memory System

**Goal:** per-project markdown files and the global SQLite/FTS5 store both work and are tested.
**Decisions:** ADR-007

- [x] `services/orchestrator/memory/`: read/append functions for `PROJECT_MEMORY.md`, `DECISIONS.md`, `LEARNINGS.md` per `project_id`
- [x] SQLite migration for the `memories` table and `memories_fts` virtual table (ARCHITECTURE.md §4.5) -- an external-content FTS5 table kept in sync via `AFTER INSERT/UPDATE/DELETE` triggers
- [x] `recall(query: str, project_id: str | None) -> list[Memory]` using FTS5 `MATCH`
- [x] Memory Curator agent (a stub/cheap LLM call is fine here): given a list of phase events, produce a markdown summary plus zero or more global memory rows -- `services/orchestrator/agents/memory_curator.py`; mocks the provider at the `model_router.resolve()` boundary in tests per `.claude/rules/testing.md`
- [x] Tests: write-then-recall round trip via FTS5; markdown appends are append-only and git-diff-friendly (no full-file rewrites for small additions)

**Definition of Done:** a scripted "fake phase" produces both an updated `LEARNINGS.md` and a queryable global memory row.

**Verification status:** fully verified for real -- `services/orchestrator/memory` and `services/orchestrator/agents` need only the standard library, pydantic, and (for the curator) `httpx`/`pyyaml` via `model_router`, all already installed. `pytest services/orchestrator/memory services/orchestrator/agents -v`: 16 passed, 99% coverage. `test_curate_and_store_phase_updates_learnings_and_records_a_queryable_memory` is the Definition-of-Done proof end to end (fake phase events -> updated `LEARNINGS.md` on disk + a `recall()`-queryable global memory row).

Along the way, fixed a cross-phase regression this phase's tests exposed: `services/orchestrator/__init__.py` (written in Phase 5) eagerly imported `service.py`, which imports `langgraph` -- so *every* submodule import under `services.orchestrator`, including this phase's memory/agents code that has no LangGraph dependency at all, failed with `ModuleNotFoundError: No module named 'langgraph'` until that eager re-export was removed. `services/orchestrator/tests/unit/test_config.py` now also passes as a result (previously blocked by the same issue).

---

## Phase 7 — Project Registry & `new-project`

**Goal:** registering a project (greenfield or brownfield) creates the right on-disk structure and registry entry.
**Decisions:** ADR-013

- [ ] `projects.yaml` schema (Pydantic) matching ARCHITECTURE.md §4.6, including per-project `coverage_threshold`, `pr_mode`, `cost_ceiling_usd_weekly` overrides
- [ ] Greenfield path: creates `projects/<id>/` with empty memory files, starts the intake graph
- [ ] Brownfield path: takes a local path or git URL, links/clones it under `projects/<id>/`, starts intake in "extend existing codebase" mode
- [ ] Validation: reject duplicate IDs, reject paths outside the `projects/` root
- [ ] Tests for both modes, including the validation failure paths

**Definition of Done:** `./foundry new-project` (and the Telegram `/new` flow) produces a correctly structured project directory and registry entry for both modes.

---

## Phase 8 — Planner / Intake Agent ("Roast the Plan")

**Goal:** structured interview → draft plan → adversarial self-critique → human approval — the same shape as the planning conversation that produced this document.
**Decisions:** ADR-001, ADR-014

- [ ] `skills/roast-the-plan/SKILL.md`: instructs the critique pass to explicitly hunt for unstated assumptions, missing edge cases, security gaps, scope creep, and untested claims
- [ ] Planner node: asks clarifying questions in small batches, incorporates answers into a running `plan` dict
- [ ] Critique pass: a second LLM call reviews the draft plan against the skill's checklist and appends findings before the human ever sees it
- [ ] Final plan rendered as a single Telegram message (or short sequence) with Approve/Edit/Reject
- [ ] Tests: a scripted sequence of user answers reaches `awaiting_plan_approval` with a complete, internally consistent plan; an intentionally-broken fixture plan is actually flagged by the critique pass (proves it isn't a no-op)

**Definition of Done:** a fixture-driven test covers the full intake-to-approval flow, including at least one critique finding that changes the final plan.

---

## Phase 9 — Coder Agent

**Goal:** implements one task at a time inside a workspace container and produces a real diff.
**Decisions:** ADR-005, ADR-012

- [ ] Coder node: pulls a task from `task_queue`, uses Executor tools (`read_file`, `write_file`, `run_command`) to implement it
- [ ] Task decomposition: plan → ordered `task_queue` (a simple LLM call is fine for v1)
- [ ] Retry budget per task; after N failed attempts, route to `blocked` and notify via Telegram rather than looping forever
- [ ] Tests: against a fixture project, assert on the Executor's recorded tool calls (not just final state) so regressions in *how* the agent works are caught, not just *whether* it eventually succeeds

**Definition of Done:** an end-to-end test takes a trivial fixture task through to a real file change inside a real workspace container.

---

## Phase 10 — Reviewer Agent

**Goal:** catches issues in a diff before tests even run.
**Decisions:** ADR-001

- [ ] Reviewer node: given a diff, produces structured comments (file, line, severity, message) plus an overall verdict (approve / changes-requested)
- [ ] Wire the verdict into the graph: changes-requested routes back to Coder with comments attached to state; approve advances to Tester
- [ ] Tests: a fixture diff with a deliberately planted bug (off-by-one, hardcoded secret, missing null check) is flagged; a clean fixture diff is approved

**Definition of Done:** both fixture cases pass reliably across at least 5 repeated runs — LLM-based checks need a consistency bar, not just a single lucky pass.

---

## Phase 11 — Tester Agent & Coverage Gate

**Goal:** runs the real test suite inside a workspace container and enforces the project's configured coverage gate.
**Decisions:** ADR-013

- [ ] Tester node calls `Executor.run_tests` (pytest + `coverage.py`)
- [ ] Parse the coverage report into `coverage_pct`
- [ ] Implement all three gate modes (hard / soft / configurable per project), defaulting new projects to hard
- [ ] On failure, route back to Coder with the specific failing tests / uncovered lines attached to state — not a generic "tests failed"
- [ ] Tests: a fixture project with deliberately low coverage trips the gate in hard mode and is allowed through with a warning in soft mode

**Definition of Done:** all three modes are covered by tests; a real `pytest --cov` run inside a workspace container produces a parsed result the gate acts on.

---

## Phase 12 — Frontend Reviewer Agent

**Goal:** visual review of UI changes, routed to a vision-capable provider.
**Decisions:** ADR-001 (capability routing)

- [ ] `skills/frontend-review/SKILL.md`: what to look for (layout breakage, basic accessibility/contrast, responsiveness at common breakpoints)
- [ ] Playwright in the workspace image (or a dedicated `workspace-frontend` image) to screenshot a running dev server
- [ ] Frontend Reviewer node: takes screenshots, calls the vision-capable provider (Claude, per `models.yaml`), returns structured findings
- [ ] Tests: a clearly-broken fixture screenshot and a clean one produce the expected verdicts; confirm the role fails loudly (not silently) if `models.yaml` points it at a provider lacking vision

**Definition of Done:** an end-to-end test captures a real screenshot of a fixture app and gets a real vision-model verdict back.

---

## Phase 13 — Git / GitHub Integrator

**Goal:** feature-branch commits always happen autonomously; PR automation follows `pr_mode`; merge-to-main is always gated.
**Decisions:** ADR-009

- [ ] Local git operations via the Executor's `run_command` only — never a separate, unscoped git tool
- [ ] GitHub API client (PyGithub or raw REST) for push, PR creation (auto mode), and PR status (manual mode just reports the branch)
- [ ] Implement the merge-to-main `interrupt()` exactly as in ARCHITECTURE.md §6.3, regardless of `pr_mode`
- [ ] Tests: auto mode opens a PR against a throwaway/test repo (mock GitHub API for unit tests; gate any real-repo integration test behind a CI secret); manual mode pushes the branch and produces the right Telegram copy; **a merge attempt without a resumed approval is provably blocked** — assert no merge call is ever made

**Definition of Done:** the "blocked without approval" test is the one that matters most here — write it first, not last.

---

## Phase 14 — Memory Curator Integration

**Goal:** the curator actually runs as part of the live graph, not just in isolation (closes the loop on Phase 6).
**Decisions:** ADR-007

- [ ] Hook curator invocation into phase-transition edges (`testing → done`, any transition into `blocked`, every approval resolution)
- [ ] Confirm a lesson learned in project A is retrievable via `recall()` while working on project B
- [ ] Tests: simulate two fixture projects hitting the same error pattern; confirm the second project's Coder agent has access to the first one's recorded fix via global recall

**Definition of Done:** the cross-project recall test passes — this is the concrete proof the global memory layer is actually useful, not just present.

---

## Phase 15 — Multi-Project Concurrency

**Goal:** more than one project can be active at once without state bleed, with sane sharing of the GPU and the global cost budget.
**Decisions:** ADR-006

- [ ] Confirm LangGraph thread isolation prevents state bleed between concurrent project threads (test: interleave two fixture projects through the graph, assert no cross-contamination of `task_queue` or `plan`)
- [ ] Serialize local-LLM-bound work through a single inference queue/semaphore (GPU is shared); Claude-API-bound work runs in parallel without this constraint
- [ ] Aggregate `cost_usd_weekly` across **all** active projects for the global cost threshold (the threshold is global, not per-project)
- [ ] Tests: two "simultaneous" projects against a mocked local provider never issue overlapping calls to it; cost aggregation correctly sums across projects and trips the global threshold

**Definition of Done:** the interleaved two-project test shows no state bleed; the serialization test proves only one local-inference call is ever in flight.

---

## Phase 16 — Skills Packaging

**Goal:** every skill referenced above exists as a portable, independently-testable `SKILL.md`.
**Decisions:** ADR-014

- [ ] Write `SKILL.md` for `develop-python`, `review-code`, `test-and-coverage`, `frontend-review`, `git-ops`, `docs-writer`, `roast-the-plan` — YAML frontmatter (`name`, `description`) + body, following Anthropic's Agent Skills format
- [ ] A small eval per skill (a handful of input/expected-behavior fixture cases, runnable in isolation from the full orchestrator)
- [ ] Verify each skill works unmodified whether the resolved provider is local or Claude API

**Definition of Done:** every skill has at least 2 passing eval cases; no skill file contains provider-specific instructions.

---

## Phase 17 — Docs, README, CV Polish

**Goal:** a stranger can clone the repo and run it from the README alone; the project reads well on a GitHub profile.
**Decisions:** ADR-003

- [ ] Full `README.md`: pitch, embedded/linked architecture diagram, quickstart (`git clone` → `./scripts/start.sh` → first Telegram message), a screenshot or short clip of a real run if possible
- [ ] One human pass to confirm `ARCHITECTURE.md` still matches what was actually built
- [ ] `CONTRIBUTING.md` and a short `SECURITY.md` summarizing §8 of `ARCHITECTURE.md`
- [ ] Confirm the LICENSE choice before making the repo public
- [ ] Tag `v0.1.0` once Phases 0–16 are green

**Definition of Done:** someone who has never seen the project can read the README and get a running stack without asking a question.

---

## Phase 18 — Security Hardening Pass

**Goal:** deliberately try to break the boundaries described in `ARCHITECTURE.md` §8 before calling v1 done.
**Decisions:** ADR-005

- [ ] Attempt to read a path outside the bind mount from inside a workspace container — confirm it fails
- [ ] Attempt to reach `docker.sock` or a denied proxy endpoint (`BUILD`/`NETWORKS`/`VOLUMES`/`SWARM`/`SYSTEM`) directly — confirm denial
- [ ] Attempt a fork bomb / resource exhaustion inside a workspace container — confirm CPU/memory limits cap it without affecting the Executor itself
- [ ] Grep all logs from a full run for literal secret values — confirm none appear
- [ ] Write findings into `services/executor/SECURITY.md`, including anything that didn't hold up the first time and how it was fixed

**Definition of Done:** every attempted breakout fails as designed; the write-up exists regardless of outcome, including any accepted residual risk.

---

## Phase 19 — Stretch / Future Work

Not required for v1. Each item should get an `ARCHITECTURE.md` update (new ADR) before implementation starts.

- [ ] MCP servers (filesystem, GitHub) as an alternative or supplement to the custom Executor tools
- [ ] Neo4j-backed cross-project pattern memory, layered under — not replacing — the markdown files
- [ ] Prometheus `/metrics` + Grafana dashboard for the platform itself
- [ ] A2A Agent Card exposure, if Foundry ever needs to interoperate with another agent system
- [ ] Polyglot workspace images beyond Python, as real projects demand them
- [ ] AMD 6750XT / ROCm-specific tuning for secondary agent roles
