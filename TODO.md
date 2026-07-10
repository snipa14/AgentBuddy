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
**Decisions:** ADR-013, ADR-018

- [x] `projects.yaml` schema (Pydantic) matching ARCHITECTURE.md §4.6, including per-project `coverage_threshold`, `pr_mode`, `cost_ceiling_usd_weekly` overrides -- lives at `/data/projects/projects.yaml` (ADR-018), a top-level YAML list per the architecture doc's example shape
- [x] Greenfield path: creates `projects/<id>/` with empty memory files, starts the intake graph -- via an `on_registered(project_id)` hook, dependency-injected so `services/orchestrator/registry` never imports LangGraph directly (same reasoning as Phase 6's `__init__.py` fix); the CLI wires the real hook with a lazy import
- [x] Brownfield path: takes a local path or git URL, links/clones it under `projects/<id>/`, starts intake in "extend existing codebase" mode -- local paths are symlinked in, git URLs are cloned via a real `git clone` subprocess call
- [x] Validation: reject duplicate IDs, reject paths outside the `projects/` root -- `project_id` is checked against a strict lowercase-alnum-and-hyphen slug pattern (an allow-list, so escaping the root is structurally impossible rather than caught after the fact)
- [x] Tests for both modes, including the validation failure paths

**Definition of Done:** `./foundry new-project` (and the Telegram `/new` flow) produces a correctly structured project directory and registry entry for both modes.

**Verification status:** fully verified for real -- `services/orchestrator/registry` needs only the standard library, pydantic, pyyaml, and a real `git` binary (used for the brownfield-from-git tests via a real local repo, no network). `pytest services/orchestrator/registry -v`: 29 passed, 97% coverage, including a real `git clone` round trip and both `git clone`/symlink failure paths. `./foundry new-project` itself is wired to `docker compose exec orchestrator python3 -m services.orchestrator.registry.cli`, but that can't actually run yet -- the orchestrator service in docker-compose.yml is still the `python:3.12-slim` / `sleep infinity` placeholder with no `services/` code or dependencies inside it, pending whichever later phase gives it a real Dockerfile. The Telegram `/new` flow mentioned in the Definition of Done isn't wired at all yet -- `services/telegram_gateway/bot.py`'s `/new` handler just forwards free text to `POST /webhook` (Phase 4), and nothing in the Orchestrator's webhook handler (Phase 5) calls into this phase's registration functions yet; that integration is left for whichever phase builds out the Planner/Intake agent's real conversational flow (Phase 8), since a real `/new` needs to gather `name`/`stack`/mode/source from conversation, not just a project_id.

---

## Phase 8 — Planner / Intake Agent ("Roast the Plan")

**Goal:** structured interview → draft plan → adversarial self-critique → human approval — the same shape as the planning conversation that produced this document.
**Decisions:** ADR-001, ADR-014, ADR-019, ADR-020

- [x] `skills/roast-the-plan/SKILL.md`: instructs the critique pass to explicitly hunt for unstated assumptions, missing edge cases, security gaps, scope creep, and untested claims
- [x] Planner node: asks clarifying questions in small batches, incorporates answers into a running `plan` dict -- `services/orchestrator/agents/planner.py` holds the testable LLM logic (ask/draft/critique/merge, each a single call); `services/orchestrator/graph/nodes.py` wires it into the subgraph as a self-looping `planner_ask`/`planner_wait` pair (one interview round per checkpointed node execution, so no LLM call is ever replayed by an `interrupt()` resume -- see that module's docstring)
- [x] Critique pass: a second LLM call reviews the draft plan against the skill's checklist and appends findings before the human ever sees it -- `critique_plan()` + `apply_critique()`; findings and the merged plan both land in the `plan_approval` interrupt payload
- [x] Final plan rendered as a single Telegram message (or short sequence) with Approve/Edit/Reject -- reuses the Gateway's one generic Approve/Reject/Edit renderer; see ADR-020 for standardizing every gate's decision vocabulary to match what that renderer actually sends (this also fixed a real bug in Phase 5's cost gate, which used "continue"/"pause" and would have silently misread a Telegram approval tap as a rejection)
- [x] Tests: a scripted sequence of user answers reaches `awaiting_plan_approval` with a complete, internally consistent plan; an intentionally-broken fixture plan is actually flagged by the critique pass (proves it isn't a no-op)

**Definition of Done:** a fixture-driven test covers the full intake-to-approval flow, including at least one critique finding that changes the final plan.

**Verification status:** the agent layer (`services/orchestrator/agents/planner.py`) is fully verified for real -- `pytest services/orchestrator/agents/tests/unit/test_planner.py -v`: 12 passed, 98% coverage, including `test_run_intake_to_final_plan_reaches_a_complete_plan_via_scripted_answers` (the Definition-of-Done scenario: scripted answers -> complete plan, with a risk *and* an assumption present only because the critique pass added them -- proving it isn't a no-op) and a caught-and-fixed real bug (a parsing mismatch between the critique response format and its parser, `_bullet_lines` vs. plain lines, that would have silently produced zero findings from every non-empty critique).

The graph layer (`node_planner_ask`/`node_planner_wait`/`node_planner_draft`/`node_planner_critique`/`node_plan_wait` in `graph/nodes.py`, wired in `graph/build.py`) is unverified, same as the rest of Phase 5 -- still blocked on `langgraph`/`langgraph-checkpoint-sqlite`/`langchain-core` not being installed in this sandbox. Every existing Phase 5 graph/service/API test was updated for the new flow (the Planner's interview now sits between `intake` and `implementing`, and `OrchestratorService`/`build_project_graph` both now take a `ModelRouter`) and hand-traced step by step against the new node wiring to check LangGraph's documented `interrupt()`/replay semantics, but none of it has actually executed. Install the three packages and run `pytest services/orchestrator -v`.

Two scope notes, not bugs: (1) "Edit" is accepted as a button but not handled as a distinct revision path in any gate yet -- treated the same as "Reject" everywhere; a real edit-and-re-plan loop is future work. (2) §6.1's sequence diagram shows project registration happening *after* plan approval, but Phase 7's registry actually runs *before* intake starts (via `new-project`, which seeds the Planner's first message from `--name`/`--stack`) -- the diagram describes an idealized steady state that isn't quite how the phases were built incrementally; not fixed here since it's outside Phase 8's scope.

---

## Phase 9 — Coder Agent

**Goal:** implements one task at a time inside a workspace container and produces a real diff.
**Decisions:** ADR-005, ADR-012, ADR-021, ADR-022

- [x] Coder node: pulls a task from `task_queue`, uses Executor tools (`read_file`, `write_file`, `run_command`) to implement it -- via a manual ReAct loop (ADR-022: no native tool-calling in model_router), dispatched through a real Executor HTTP surface built this phase (ADR-021, `services/executor/api.py` + `services/orchestrator/executor_client.py`) since nothing before Phase 9 needed the Orchestrator to actually call the Executor over the network
- [x] Task decomposition: plan → ordered `task_queue` (a simple LLM call is fine for v1) -- `decompose_plan_into_tasks()`, one node (`node_task_decomposition`) run once right after plan approval
- [x] Retry budget per task; after N failed attempts, route to `blocked` and notify via Telegram rather than looping forever -- `MAX_TASK_ATTEMPTS = 3`, tracked in `current_task["attempts"]` (no new ProjectState field needed, it's already a free-form dict). **The "notify via Telegram" half is not actually wired** -- see the new Decision Log/§11 note below, this is a pre-existing gap across every gate, not something Phase 9 introduces or could reasonably close alone
- [x] Tests: against a fixture project, assert on the Executor's recorded tool calls (not just final state) so regressions in *how* the agent works are caught, not just *whether* it eventually succeeds -- `RecordingExecutorClient` in `tests/unit/test_coder.py` asserts on the exact call sequence; the real-Docker integration test does the same against actual Executor HTTP + Docker

**Definition of Done:** an end-to-end test takes a trivial fixture task through to a real file change inside a real workspace container.

**Verification status:** the agent layer and Executor's new HTTP surface are fully verified for real. `pytest services/orchestrator/agents/tests/unit/test_coder.py services/orchestrator/tests/unit/test_executor_client.py services/executor/tests/unit/test_api.py -v`: 10 + 7 + 14 = 31 passed (plus the untouched existing suites all still green: 148 passed total across model_router/memory/agents/registry/executor unit/orchestrator config+executor_client). Coverage numbers aren't reported this round -- `pytest-cov` became transiently uninstalled partway through this session (concurrent package-install activity in this sandbox) and didn't come back before this phase wrapped up; every test that ran, ran for real and passed, just without a coverage percentage attached.

The real-Docker integration test (`services/orchestrator/agents/tests/integration/test_coder_end_to_end.py`) skips cleanly here (no Docker daemon), same as Phase 3's suite -- written and hand-traced but not yet run against a live daemon.

The graph-layer wiring (`node_task_decomposition`, `node_coder`, `route_after_coder` in `graph/nodes.py`/`build.py`, plus `OrchestratorService`/`OrchestratorConfig`/`main.py`/`registry/cli.py` all threading an `executor_client` through now) remains unverified, same as the rest of Phase 5/8 -- still blocked on `langgraph`/`langgraph-checkpoint-sqlite`/`langchain-core`, which still aren't installed in this sandbox as of this phase. Every existing graph/service/API test was updated and hand-traced step by step against the new task_decomposition -> coder self-loop -> cost_check wiring (see `graph/tests/unit/planner_fakes.py`'s extended `fast_track_router()`, which now also scripts the Coder saying "done" on its first turn so those tests never need a working `ExecutorClient`).

---

## Phase 10 — Reviewer Agent

**Goal:** catches issues in a diff before tests even run.
**Decisions:** ADR-001

- [x] Reviewer node: given a diff, produces structured comments (file, line, severity, message) plus an overall verdict (approve / changes-requested)
- [x] Wire the verdict into the graph: changes-requested routes back to Coder with comments attached to state; approve advances onward (currently to `frontend_review`, Phase 12's node, which itself falls through to `cost_check`/Tester when there are no frontend files -- see the Phase-discipline note below)
- [x] Tests: a fixture diff with a deliberately planted bug (off-by-one, hardcoded secret, missing null check) is flagged; a clean fixture diff is approved

**Definition of Done:** both fixture cases pass reliably across at least 5 repeated runs — LLM-based checks need a consistency bar, not just a single lucky pass.

**Verification status:** agent layer fully verified for real -- `pytest services/orchestrator/agents/tests/unit/test_reviewer.py -v`: 9 passed (grew from 7 after a validation pass below added a missing-severity/missing-file coverage case). The full non-graph test suite (172 tests across agents/memory/registry/executor/model_router) is also green. Graph-layer wiring (`node_reviewer`/`route_after_reviewer` in `graph/nodes.py`, wired in `graph/build.py`) and node unit tests are still unverified, same as the rest of Phase 5 -- still blocked on `langgraph`/`langgraph-checkpoint-sqlite`/`langchain-core` not being installed in this sandbox.

A validation pass on this phase found and fixed four real defects before this note was written, all now fixed and covered by tests:
1. `graph/tests/unit/test_nodes.py`'s `route_after_reviewer` test asserted the approve path returns `"cost_check"`, but `graph/nodes.py` actually routes to `"frontend_review"` (Phase 12's node, inserted ahead of cost_check) -- the test was stale relative to the code it claimed to cover. Fixed to assert the real target.
2. `services/orchestrator/agents/reviewer.py`'s `review_changes()` called `provider.complete()` with a plain `dict` instead of a `CompletionRequest`/`CompletionMessage` pair -- invisible to the mocked unit suite (the `FakeRouter` ignores request shape entirely) but a guaranteed crash against any real provider. Caught by actually attempting to write the Definition of Done's real-model reliability test (see below) and fixed to match the Planner/Coder's `CompletionRequest` pattern.
3. `_parse_review_result`'s docstring claimed it raises `ValueError` on an invalid finding severity; the code silently dropped the bullet instead, untested either way. Fixed to log a warning on drop (so malformed findings leave a trace instead of vanishing silently) and corrected the docstring to describe that behavior; added tests for both the bad-severity and missing-file drop paths.
4. `model_router/providers/anthropic.py`'s `_build_messages` (added for Phase 12 vision support) always wrapped message `content` in a list, even for plain text -- this broke the previously-green `test_anthropic_provider_complete_returns_completion_result` (confirmed via `git stash`: passes at the Phase 9 commit, failed on this working tree). Fixed to only use the multi-content-block array format when a message actually has images, matching `ollama.py`'s equivalent change; added a regression test for the image-block path.

The Definition of Done's "5 repeated runs" bar is now backed by a real test -- `services/orchestrator/agents/tests/integration/test_reviewer_reliability.py` runs `review_changes()` against a real Claude provider 5x on a hardcoded-credential fixture and 5x on a clean fixture, asserting >=4/5 reliability each way (per `.claude/rules/testing.md`'s explicit bar for Reviewer/Tester, since the unit suite's mocked provider can only prove parsing logic, not that a real model actually catches the bug). It's gated behind an explicit `FOUNDRY_RUN_REVIEWER_RELIABILITY_TEST=1` env var, not just presence of a credential -- a first version gated on credential-presence alone fired for real against `api.anthropic.com` in this sandbox because `ANTHROPIC_API_KEY` happens to always be set here (it's the Claude Code session's own key, not a Foundry deployment secret); that call 400'd before generating any output, but the near-miss is why the stricter opt-in gate exists now. **Not yet run for real** -- verified only that it collects and constructs its `CompletionRequest` correctly; running it for real against a live Claude API key (spending real quota) is left for whoever next has a deployment key to spend, per this session's user decision.

Phase-discipline note: this working tree bundles Phase 10 (Reviewer), Phase 11 (Tester), and Phase 12 (Frontend Reviewer, plus `skills/frontend-review/SKILL.md` and vision plumbing across `anthropic.py`/`ollama.py`/`schemas.py`) all together, uncommitted -- against this repo's own "one phase at a time" rule. Defect #4 above is a direct, measured cost of that (Phase 12 work broke a Phase 2 test). This validation pass fixed what it found without unwinding that bundling, since disentangling three interleaved phases' worth of graph wiring was judged riskier than leaving a note; Phase 11/12's own gaps (e.g. `frontend_reviewer.py`'s one failing unit test, and `node_testing`'s missing conditional edge back to Coder on test failure) are out of this phase's scope and still open.

---

## Phase 11 — Tester Agent & Coverage Gate

**Goal:** runs the real test suite inside a workspace container and enforces the project's configured coverage gate.
**Decisions:** ADR-013, ADR-023

- [x] Tester node calls `Executor.run_tests` (pytest + `coverage.py`)
- [x] Parse the coverage report into `coverage_pct`
- [x] Implement all three gate modes (hard / soft / configurable per project), defaulting new projects to hard
- [x] On failure, route back to Coder with the specific failing tests / uncovered lines attached to state — not a generic "tests failed"
- [x] Tests: a fixture project with deliberately low coverage trips the gate in hard mode and is allowed through with a warning in soft mode

**Definition of Done:** all three modes are covered by tests; a real `pytest --cov` run inside a workspace container produces a parsed result the gate acts on.

**Verification status:** agent layer fully verified for real -- `pytest services/orchestrator/agents/tests/unit/test_tester.py -v`: 17 passed (grew from 11 after the validation pass below), covering all three gate modes (hard blocks low coverage, soft warns and passes, configurable blocks), test failure parsing, uncovered-line parsing, and end-to-end with mocked executor client. The full non-graph test suite (128 tests across agents/memory/registry/model_router + 34 passed/6 skipped in executor, the 6 skips being the real-Docker integration tests with no daemon here) is also green. Graph-layer wiring (`node_testing` in `graph/nodes.py`, wired in `graph/build.py`) and node unit tests remain unverified by actually running them, same as the rest of Phase 5 -- still blocked on `langgraph`/`langgraph-checkpoint-sqlite`/`langchain-core` not being installed in this sandbox; every touched graph test was hand-traced instead, same practice as every prior phase's graph layer.

A validation pass on this phase found and fixed four real defects, all now fixed and covered by tests:
1. **The headline bug**: `node_testing` derived the coverage gate mode from `pr_mode` (`auto`→hard, `manual`→soft, `configurable`→configurable) instead of having its own setting. `pr_mode` is ARCHITECTURE.md's field for GitHub PR automation (§4.7, Phase 13) — completely unrelated to coverage strictness — and this mapping meant a new project (default `pr_mode="manual"`) silently got a **soft** coverage gate, directly violating this phase's own Definition of Done ("defaulting new projects to hard"). Fixed by adding a proper `coverage_gate_mode` field to `ProjectState` and the project registry (`registry/schemas.py`, `registry/registration.py`, `registry/cli.py`, `graph/state.py`), defaulting to `"hard"`, independent of `pr_mode` — see ADR-023. The two `graph/tests/unit/test_nodes.py` tests that had encoded the old conflation as expected behavior (selecting hard/soft mode via `pr_mode=` instead of a real gate-mode field) were rewritten to use `coverage_gate_mode=` directly, plus a new test proving a project can have `pr_mode="auto"` and `coverage_gate_mode="soft"` at once (ADR-023's whole point).
2. `_evaluate_gate`'s two log statements formatted `"project %s: ..."` but the function has no `project_id` parameter, so they always logged the literal string `"project 0"` — dead/misleading log data on every coverage-shortfall run. Fixed by dropping the fabricated prefix (the caller, `run_tests()`, already logs the real project_id).
3. `_parse_failing_tests` unconditionally set `TestFailure.line=0` even though no line number is ever actually parsed from pytest's `FAILED path::test - message` summary format — a fabricated value indistinguishable from a real line 0, worse than the field's own `None` default. Fixed to leave it `None` when genuinely unknown.
4. The DoD's "uncovered lines attached to state" requirement was only partially met — a coverage-shortfall failure told the Coder the aggregate percentage but never which lines were actually uncovered, even though `--cov-report=term-missing` (already the flag Executor's `tool_run_tests` uses) puts exactly that in the report. Added `_parse_uncovered_lines()` (parses coverage.py's per-file `Missing` column) and wired it into `node_testing`'s Coder feedback alongside the failing-tests case.

---

## Phase 12 — Frontend Reviewer Agent

**Goal:** visual review of UI changes, routed to a vision-capable provider.
**Decisions:** ADR-001 (capability routing), ADR-024

- [x] `skills/frontend-review/SKILL.md`: what to look for (layout breakage, basic accessibility/contrast, responsiveness at common breakpoints)
- [x] Screenshot tool: headless Chromium via Playwright inside the workspace container (ADR-024); supports auto-starting dev server, multiple breakpoints
- [x] Vision schema extension: `CompletionMessage.images` field with base64-encoded images; both AnthropicProvider and OllamaProvider serialize correctly
- [x] Frontend Reviewer node: checks for frontend files, takes screenshots at desktop/mobile breakpoints, calls vision-capable provider (Claude Sonnet per `models.yaml`), returns structured findings
- [x] Executor API: new `/take_screenshot` endpoint with `ScreenshotResult` schema
- [x] Tests: 15 unit tests in `test_frontend_reviewer.py` (parse paths, review end-to-end, frontend file detection, system-prompt/auto-start assertions) + 4 in `test_tools_mocked.py` covering `tool_take_screenshot` + 3 API-layer tests for `/take_screenshot`, plus a real-Docker/real-Claude-vision end-to-end integration test

**Definition of Done:** an end-to-end test captures a real screenshot of a fixture app and gets a real vision-model verdict back.

**Verification status:** agent layer fully verified for real -- `pytest services/orchestrator/agents/tests/unit/test_frontend_reviewer.py services/executor/tests/unit/test_tools_mocked.py -v`: 32 passed. The full non-graph test suite (168 tests across agents/memory/registry/model_router + 34 passed/6 skipped in executor) is also green. `services/executor/tests/unit/test_api.py`'s new `/take_screenshot` tests are written and hand-traced against `api.py`'s actual `TakeScreenshotRequest` schema but unverified in this sandbox -- `fastapi` isn't installed here, same pre-existing gap as every other `test_api.py` run this session. Graph-layer wiring (`node_frontend_review`/`route_after_frontend_review` in `graph/nodes.py`, wired in `graph/build.py`) and their new node-level unit tests are unverified by actually running them, same as the rest of Phase 5+ -- still blocked on `langgraph`/`langgraph-checkpoint-sqlite`/`langchain-core` not being installed in this sandbox; hand-traced instead.

A validation pass on this phase found and fixed four real defects, all now fixed and covered by tests:
1. **The headline bug**: `tool_take_screenshot`'s original implementation never rendered anything -- it fetched the target URL's raw HTTP response bytes and passed them straight to `PIL.Image.open()`, which can only decode a response that's already an image file. Any real HTML/CSS/JS page (i.e. every actual use case) made this raise `PIL.UnidentifiedImageError`, silently caught and reported as a generic screenshot failure. This also contradicted ARCHITECTURE.md §4.7, which specified "screenshots via Playwright" from the start -- the implementation had swapped in urllib+Pillow without updating the doc to match. Fixed per ADR-024: `images/workspace-python/Dockerfile` now installs Playwright + headless Chromium (to a world-readable `PLAYWRIGHT_BROWSERS_PATH`, launched with `--no-sandbox --disable-dev-shm-usage` since the workspace container's locked-down runtime constraints rule out Chromium's own setuid sandbox), and `_TOOL_TAKE_SCREENSHOT_PY` actually launches a browser, navigates to the page, and captures a real full-page screenshot.
2. `review_frontend()` built the vision system prompt (`_build_image_message()`, with the full "what to look for" instructions and required response format) but never attached it to the request actually sent to the model -- the result of that call was assigned to a variable and never used. Every real request went out as bare image-label text with zero instructions, making the `## Verdict` / `## Findings` response format the parser expects essentially unenforceable. Fixed by sending a real system message (matching the Reviewer agent's established pattern) alongside the images; `_build_image_message()` (dead code) removed.
3. `review_frontend()` never passed `auto_start_server=True` to `take_screenshot()`, even though every screenshot call runs inside a fresh, ephemeral workspace container (ARCHITECTURE.md §4.3) with nothing ever already listening -- so every real screenshot attempt would fail with `url_not_ready`, and the "no screenshots captured" fallback would silently auto-approve every review. Fixed to always request an auto-started server.
4. `_has_frontend_files()` swallowed every exception from `list_files()` with a bare `except Exception: return False` and no log line, violating this repo's "catch Exception with a log" rule and masking real Executor/Docker failures as "no frontend files, skip review." Fixed to log a warning before returning False; also simplified away an unnecessary `ThreadPoolExecutor` wrapper (the only caller is a plain sync node function with no event loop already running, so a direct `asyncio.run()` is sufficient, per ADR-019's pattern).

A fifth, smaller bug found in the same pass: `node_frontend_review` wrapped `_build_findings_text()`'s output (which already starts with "Frontend review feedback:") in another "Frontend review feedback:\n{...}" f-string, producing a duplicated header in every changes-requested message sent back to the Coder. Fixed to use the helper's output directly.

The Definition of Done's real end-to-end proof is `services/orchestrator/agents/tests/integration/test_frontend_reviewer_e2e.py` -- real Docker (same in-process-ASGI pattern as `test_coder_end_to_end.py`) serving a fixture page with a deliberately broken visual (white text on a white background, overlapping buttons) and a real Claude vision call, gated behind `FOUNDRY_RUN_FRONTEND_REVIEWER_E2E_TEST=1` (same opt-in pattern as `test_reviewer_reliability.py`, for the same reason: credential presence alone isn't sufficient signal to spend real API quota). **Not yet run for real** -- no Docker daemon in this sandbox, and the Dockerfile's Playwright/Chromium install hasn't been built or exercised against a live daemon either. Build the image and run this test on a Docker-enabled machine with a Claude API key before treating this phase's Definition of Done as fully met.

---

## Phase 13 — Git / GitHub Integrator

**Goal:** feature-branch commits always happen autonomously; PR automation follows `pr_mode`; merge-to-main is always gated.
**Decisions:** ADR-009

- [x] Local git operations via the Executor's `run_command` only — never a separate, unscoped git tool
- [x] GitHub API client (PyGithub or raw REST) for push, PR creation (auto mode), and PR status (manual mode just reports the branch)
- [x] Implement the merge-to-main `interrupt()` exactly as in ARCHITECTURE.md §6.3, regardless of `pr_mode`
- [x] Tests: auto mode opens a PR against a throwaway/test repo (mock GitHub API for unit tests; gate any real-repo integration test behind a CI secret); manual mode pushes the branch and produces the right Telegram copy; **a merge attempt without a resumed approval is provably blocked** — assert no merge call is ever made

**Definition of Done:** the "blocked without approval" test is the one that matters most here — write it first, not last.

**Verification status:** the graph-layer suite (`services/orchestrator/graph/tests/unit/test_integrator.py`) is written and hand-traced but unverified by actually running it in this sandbox -- `langgraph`/`pygithub` aren't installed here and the host-install hook (`.claude/rules/security.md`, `block_host_installs.py`) correctly blocks installing them outside a workspace container, the same gap every graph-layer phase since Phase 5 has documented. Install them and run `pytest services/orchestrator/graph/tests/unit/test_integrator.py -v` on a machine that has them.

A validation pass on the uncommitted first implementation found and fixed several real defects before this note was written:
1. **`build_project_graph` couldn't be called at all**: `graph/build.py` used `node_intake` and `node_frontend_review` (both already required by earlier phases) without importing them -- a `NameError` on the very first `build_project_graph()` call, in every deployment and every test. Fixed by adding both back to the import list.
2. **Two conditional-edge routing tables were out of sync with the node functions they route**: `route_after_cost_wait` returns `"memory_curator"` but `build.py`'s map for the `cost_wait` node still only mapped `{"testing": "testing", END: END}` (a stale mapping from before the Memory Curator was wired in); `route_after_memory_curator` can return `"coder"` (test-failure retries) but the `memory_curator` node's map didn't include that key at all. Either would have raised a routing error the first time a real run actually took that path. Fixed both maps.
3. **`IntegratorClient.commit_and_push` never actually worked**: it called `Executor.run_command` with a single shell string (e.g. `"git config user.email '...' && git config user.name '...'"`), but `run_command`'s real contract is an argv list executed directly via the Docker SDK's `exec_run` with no shell involved (`services/executor/containers.py`'s `exec_in_container`) -- so `&&` was never interpreted, and even a single git subcommand as one string would fail (Docker would try to exec a binary literally named that whole string). It also treated the returned `CommandResult` object as if it were a plain string (`.strip()` on a Pydantic model) and used a nonexistent git flag (`git rev-parse --is-init`) to detect an existing repo. None of this was caught before because the file's own test suite mocked `run_command` with a fake that accepted a string and returned strings -- a fake that didn't match the real interface at all (".claude/rules/security.md" calls this pattern out by name for the Docker boundary; the same principle applies here). Rewrote every call as a real argv list, checks `CommandResult.exit_code` instead of parsing stdout, and drops the invalid-flag repo check (`git init` on an already-initialized repo is a safe no-op, so there's nothing to detect).
4. **`git remote add origin` failing on a second run**: a project's feature branch is recreated on every merge, but `git remote add` errors if `origin` already exists from a prior run. Fixed to fall back to `git remote set-url` when `add` fails.
5. **`GITHUB_TOKEN` was read once at module import time** (`_GITHUB_TOKEN = os.environ.get(...)` as a module-level constant), so a test (or a real deployment) setting the env var after the module first loads would never see it -- `create_pull_request` would always see whatever value was present the first time anything imported `integrator_client.py`, which in this sandbox's test run order is usually empty, silently failing every "PR created successfully" test case. Fixed to read `os.environ.get("GITHUB_TOKEN", "")` inside `create_pull_request` at call time.
6. **The Integrator's entire git-commit path was dead code in every real run**: `node_done` originally no-op'd unless *both* `git_branch` and `git_remote_url` were already set on `ProjectState`, but nothing anywhere in the graph ever set either field (they're new fields this phase added) -- so despite everything else above being fixed, a real project would still never commit anything, directly contradicting this phase's own goal ("feature-branch commits always happen autonomously"). Fixed per ADR-029: `git_branch` now defaults to `feature/{project_id}` (always mechanically derivable, no missing wiring needed), so a local commit always happens autonomously; `git_remote_url` is left unset by default since threading the project registry's `git_remote` field into `ProjectState` is a separate, pre-existing gap Phase 7/8 already flagged and deferred, not something to silently fold into this phase.
7. **`route_after_plan_wait`/`route_after_cost_wait`/`route_after_merge_wait` all short-circuited straight to `END` on a rejected/blocked approval**, bypassing the `memory_curator` node entirely -- meaning "any transition into blocked" (Phase 14's own stated requirement, see below) never actually triggered curation for these three gates. Fixed to always route to `memory_curator` regardless of the decision; `route_after_memory_curator`'s own blocked-phase check now does the halting, *after* curation has a chance to run.
8. `route_after_memory_curator`'s return type annotation (`str | Literal["END"]`) used `Literal` without importing it from `typing` -- a `NameError` at `graph/nodes.py`'s import time, meaning literally nothing in the module (or anything importing it) could load. Fixed to `-> str`, matching every other `route_*` function's annotation in the file (`END` is a sentinel object, not the literal string `"END"`).

---

## Phase 14 — Memory Curator Integration

**Goal:** the curator actually runs as part of the live graph, not just in isolation (closes the loop on Phase 6).
**Decisions:** ADR-007

- [x] Hook curator invocation into phase-transition edges (`testing → done`, any transition into `blocked`, every approval resolution)
- [x] Confirm a lesson learned in project A is retrievable via `recall()` while working on project B
- [x] Tests: simulate two fixture projects hitting the same error pattern; confirm the second project's Coder agent has access to the first one's recorded fix via global recall

**Definition of Done:** the cross-project recall test passes — this is the concrete proof the global memory layer is actually useful, not just present.

**Verification status:** same sandbox limitation as Phase 13 -- `services/orchestrator/graph/tests/unit/test_memory_curator.py` is written and hand-traced but unverified by actually running it here (`langgraph` not installed, host-install hook correctly blocks installing it). `services/orchestrator/memory`'s own suite (no LangGraph dependency) is verified for real -- `pytest services/orchestrator/memory -v` passes as part of this session's `pytest services/executor --ignore=services/executor/tests/unit/test_api.py services/orchestrator/memory services/orchestrator/registry` run (79 passed, 6 skipped).

A validation pass found and fixed real defects beyond the shared `route_after_*`/`Literal`-import bugs already listed under Phase 13 (both phases' graph wiring landed in the same working tree, per this session's git.md-noted bundling):
1. **`node_coder`'s exhausted-retries transition into `blocked` never went through the curator at all** -- `route_after_coder` routed straight to `END`, and `node_coder` never set `curator_source`. This is exactly the "any transition into blocked" case Phase 14's own task list calls out, but the Coder's blocked path was the one transition into `blocked` this phase's original implementation missed (`node_plan_wait`/`node_cost_wait`/`node_merge_wait` all correctly set `curator_source` on rejection, just couldn't reach the curator node due to bug #7 above). Fixed: `node_coder` now sets `curator_source="coder"` when blocking, and `route_after_coder` routes to `memory_curator` instead of `END` directly.
2. **`node_memory_curator` never accounted for its own LLM call's cost** -- it always returned `{}` on success, even though curation is itself a real, billed completion (Phase 15's cost-tracking requirement covers every LLM-backed node, and the curator is one). Fixed to fold the curation call's `cost_usd` into `cost_usd_session`/`cost_usd_weekly`, same as every other LLM-backed node -- while still returning a literal `{}` (not zero-valued cost keys) when curation didn't run at all, so the existing "no-op" test assertions stay meaningful.
3. The test file's own `_FakeCuratorRouter` set `mock.complete.return_value = MagicMock(...)` on a plain (non-async) `MagicMock` -- `await`ing that call would have raised `TypeError`, silently caught by `node_memory_curator`'s broad `except Exception`, meaning `test_node_memory_curator_runs_curation_and_stores` would never actually have exercised a successful curation even before Phase 15 replaced this fake entirely. Superseded by Phase 15's `BaseChatModel`-shaped fakes (`planner_fakes.py`'s `FakeChatModel`/`ScriptedStructured`), which don't have this problem since `with_structured_output(...).ainvoke()` is a real `async def`.

---

## Phase 15 — Model Router: LangChain Migration & Structured Output

**Goal:** replace the custom `Provider` protocol with LangChain's `BaseChatModel` across every existing agent, so tool-calling, retry, and per-role structured output come from a maintained integration instead of hand-rolled markdown parsing -- and make the cost-threshold gate (Phase 5, ADR-009) actually trip from real usage instead of a field that never changes from its initial value.
**Decisions:** ADR-025 (supersedes ADR-022's rationale), ADR-026

- [x] `pyproject.toml`: add `langchain-anthropic`, `langchain-ollama` dependencies
- [x] `model_router/provider.py` (the custom `Provider` Protocol) and the hand-rolled `providers/anthropic.py`/`providers/ollama.py` removed
- [x] `model_router/router.py`: `resolve(role: str) -> BaseChatModel`, constructing `ChatAnthropic(model=..., api_key=..., max_retries=5)` / `ChatOllama(model=..., base_url=..., max_retries=5)` per configured provider entry; existing capability/prefer-chain matching logic and `NoCapableProviderError`/`UnknownRoleError` behavior preserved unchanged (only the constructed object type changes)
- [x] `config/models.yaml`: add `cost_per_1m_input_tokens`/`cost_per_1m_output_tokens` to every provider entry (Pydantic schema validation extended to require them)
- [x] Cost tracking: `ModelRouter.cost_usd(role, usage_metadata)` looks up the resolved provider's per-token pricing and returns a `cost_usd` delta; every LLM-backed agent function returns `(result, raw_ai_message)` so the calling graph node -- not the agent code itself -- can price the call and fold the delta into its own `cost_usd_session`/`cost_usd_weekly` state update (`graph/nodes.py`'s `_with_cost` helper); the Memory Curator is included (it makes a real, billed call too) even though it wasn't explicitly named in this phase's original task list
- [x] Structured output migration: `Planner` (`draft_plan`/`critique_plan`/`ask_clarifying_questions`), `Reviewer` (`review_changes`), `Frontend Reviewer` (`review_frontend`), and `Memory Curator` (`curate_phase`, added beyond the original list once `Provider`'s removal made its `.complete()` call unusable) switch from `agents/parsing.py`'s `split_sections`/`bullet_lines` markdown parsing to `llm.with_structured_output(schema, include_raw=True)` via a shared `agents/llm.py::invoke_structured` helper; `agents/parsing.py` deleted (nothing references it anymore, including the Coder -- see below)
- [x] Coder tool-calling migration: `run_coder_task` switches to `llm.bind_tools([read_file, write_file, run_command])` (LangChain `@tool`-decorated functions closed over one project's `ExecutorClient`); task completion is signaled by the model returning a turn with no further tool calls; `decompose_plan_into_tasks` also migrated to structured output (not explicitly listed in the original task breakdown, but it used the same soon-to-be-deleted `Provider.complete()` call and needed to move too)
- [x] Test fakes: `agents/tests/unit/router_fakes.py`'s `FakeRouter`/`ScriptedProvider` and `graph/tests/unit/planner_fakes.py`'s duplicate copy rewritten to fake `BaseChatModel`'s surface (`.ainvoke`, `.bind_tools`, `.with_structured_output`) instead of a custom `.complete()`; every agent unit test (`test_planner.py`, `test_coder.py`, `test_reviewer.py`, `test_frontend_reviewer.py`, `test_memory_curator.py`) and every graph-layer test that drives the fast-track flow updated accordingly
- [x] Tests: cost-accumulation tests in `model_router/tests/unit/test_router.py` (`cost_usd()` computed from scripted `usage_metadata`, including that it resolves the same provider `resolve()` would); a construction-time assertion that every `ChatAnthropic`/`ChatOllama` the router builds has `max_retries` set; structured-output round trips for each migrated role via `ScriptedStructured`, including a parsing-failure case that asserts `invoke_structured` raises rather than silently drops; Coder tool-calling round trips scripting `AIMessage.tool_calls` sequences, matching the exact-call-sequence assertion style already used in `test_coder.py`

**Definition of Done:** every existing agent's test suite passes against the new `BaseChatModel`-based router with zero references to the old `Provider` protocol remaining anywhere in `services/`; a real (or realistically mocked) Claude API response's token usage produces a nonzero `cost_usd_weekly` the existing cost gate (`node_cost_check`) can actually trip on.

**Verification status:** implemented and hand-traced, but **not verified by actually running the migrated test suites in this sandbox** -- `langchain-anthropic`/`langchain-ollama`/`langgraph`/`fastapi`/`pygithub` are none of them installed here, and `.claude/rules/security.md`'s host-install hook correctly blocks installing any of them outside a workspace container (confirmed by attempting it: `block_host_installs.py` fired as designed). This is the same gap every LangGraph-touching phase since Phase 5 has carried, now extended to the model_router/agents layer too, since it depends on `langchain-anthropic`/`langchain-ollama` for the first time. What *is* verified for real in this session: every touched file byte-compiles (`python3 -m py_compile`) and passes an AST-level unused-import scan; `pytest services/executor --ignore=services/executor/tests/unit/test_api.py services/orchestrator/memory services/orchestrator/registry` (the suites with no LangChain/LangGraph/FastAPI dependency) is green at 79 passed, 6 skipped, confirming this phase's changes didn't regress anything outside its own footprint. `grep` swept the whole repo for every deleted symbol (`CompletionRequest`/`CompletionMessage`/`CompletionResult`/`ImageContent`/`TokenUsage`/`ProviderRequestError`/`AnthropicProvider`/`OllamaProvider`/`agents.parsing`/`decide_next_action`/`CoderAction`/etc.) and fixed every real reference found, including one in `services/orchestrator/agents/__init__.py` that would have broken the package's own import. Install the five packages and run `pytest services/orchestrator -v` on a machine that has them before treating this phase's Definition of Done as fully met.

---

## Phase 16 — Live Progress Streaming to Telegram

**Goal:** close the proactive-notification gap flagged in ARCHITECTURE.md §11 ("no push or poll mechanism exists yet") and give the Coder's long turns visible incremental progress in Telegram instead of silence until the whole turn completes.
**Decisions:** ADR-027

- [x] Orchestrator: a new `GET /stream/{project_id}` endpoint returning the latest buffered progress chunk(s) for a project's currently-running node, plus a monotonic sequence number so the Gateway only ever requests what's new
- [x] Coder node: switches its LLM call from `ainvoke` to `astream`; partial content is appended to a per-project progress buffer as it streams (LangGraph nodes still commit their actual state update only once, on return -- the buffer is separate, ungated, read-only-for-the-Gateway state)
- [x] Telegram Gateway: a background poller task started when a project's webhook/`/new` flow begins and stopped once the project reaches a terminal phase (`done`/`blocked`) or goes idle for some window; polls the stream endpoint every ~3s and relays new chunks as Telegram messages, coalescing rapid updates into one edited message rather than one message per chunk
- [x] `ARCHITECTURE.md` §11: remove the "no push or poll mechanism exists yet" gap note -- this phase closes it
- [x] Tests: a fake Orchestrator stream endpoint returning scripted chunks across repeated polls; assert the Gateway poller relays them in order, stops polling once the project reaches a terminal phase, and never re-sends a chunk already relayed (sequence-number correctness)

**Definition of Done:** a live Coder turn against a real (or scripted, streaming-capable) provider produces at least one Telegram message before that turn's underlying LLM call returns.

**Verification status:** this phase was found uncommitted and incomplete at the start of this session (only `services/orchestrator/api.py` and a rough `progress_buffer.py` stub touched) and was completed as part of this session alongside Phase 17. `services/orchestrator/progress_buffer.py`'s own suite is verified for real in this sandbox -- `pytest services/orchestrator/tests/unit/test_progress_buffer.py`: 5 passed. `services/orchestrator/api.py`'s new `/stream` endpoint and `service.py`'s terminal-phase buffer-clearing are hand-traced but unverified by running (`fastapi`/`langgraph` gaps, same as every other `test_api.py`/`test_service.py` run this session -- though notably `fastapi` *was* importable for the Executor's own `test_api.py` this session, just not paired with `langgraph`, which `services/orchestrator/api.py` transitively needs via `service.py`). The Coder's `astream` loop and Gateway's `ProgressPoller`/`bot.py` wiring are hand-traced and unverified for the same reason (`langchain_core`/`aiogram` not installed here). Two real bugs were found and fixed during this validation pass, independent of streaming itself: (1) `ModelRouter`'s constructor call sites in `main.py` and `registry/cli.py` still passed a stale positional `httpx.AsyncClient()` argument left over from before Phase 15's constructor signature change, which would have silently miswired Ollama enablement in the real deployed entrypoint; (2) `render_approval()` (`telegram_gateway/rendering.py`) was fully built and unit-tested in Phase 4/5 but never actually called from any `bot.py` handler, so the entire Approve/Reject/Edit keyboard was dead code end-to-end until this pass wired it into `handle_command`/`handle_free_text`; (3) `ProgressPoller._relay` concatenated every streamed chunk into one ever-growing message with no cap, which would have hit Telegram's 4096-character message limit and started raising on any sufficiently long Coder turn -- fixed by truncating only what's sent to Telegram (keeping the latest content, prefixed with a truncation marker) while still accumulating the full text internally, with a new test (`test_poll_once_truncates_the_telegram_message_but_keeps_the_full_text_internally`) proving both halves. Install `langgraph`, `langchain-core`, `langchain-anthropic`, `aiogram`, `fastapi` together and run `pytest services/orchestrator services/telegram_gateway -v` on a machine that has them before treating this phase as fully proven.

---

## Phase 17 — Polyglot Workspace Images & Dependency Install

**Goal:** the Coder can build in Python or Node.js projects without granting any agent-reachable code path the ability to build or customize a Docker image -- ADR-005's `docker-socket-proxy` `BUILD=0` boundary stays exactly as it is.
**Decisions:** ADR-028

- [x] `images/workspace-node/Dockerfile`: Node.js base image mirroring `images/workspace-python`'s security posture exactly (non-root `sandbox` user, capability-stripped, pinned base tag, no unnecessary setuid binaries)
- [x] `registry/schemas.py`: `stack` becomes `Stack = Literal["python", "node"]` (currently a free `str`); `registry/cli.py`'s `--stack` argument gets `choices=["python", "node"]`
- [x] `services/executor/`: `workspace_image` becomes a per-call parameter derived from the project's `stack`, not the single Executor-wide `ExecutorConfig.workspace_image`/`WORKSPACE_IMAGE` env var it is today -- an `image_for_stack(stack: str) -> str` lookup the Executor owns (`"python"` -> `images/workspace-python`, `"node"` -> `images/workspace-node`), with `ExecutorConfig.workspace_image` kept as the fallback for any stack without a dedicated image yet
- [x] `services/orchestrator/executor_client.py`: every method threads the project's `stack` (already known via the registry / `ProjectState`) through to the Executor's HTTP payload
- [x] `services/executor/containers.py`: `workspace_container()` mounts a second, per-project named Docker volume (`foundry-cache-{project_id}`) at the workspace image's package-manager cache directory (`~/.cache/pip` for Python, `~/.npm` for Node) alongside the existing project bind mount; `.claude/rules/security.md`'s "Workspace container launch checklist" updated to explicitly allow this one additional named volume (still zero host paths outside the project directory -- a Docker-managed named volume isn't a host path)
- [x] Manifest-detection + auto-install: before a workspace container's requested tool call executes, the Executor checks for a known manifest file at the project root (`requirements.txt`/`pyproject.toml` for python, `package.json` for node) and runs the matching install command (`pip install -r requirements.txt`, `npm install`) first, relying on the package manager's own cache-hit behavior against the mounted cache volume to make repeat installs with no dependency changes fast rather than re-downloading every time
- [ ] Cache volume cleanup: when a project is archived/removed from the registry, the Executor removes that project's cache volume alongside its existing container cleanup -- **partially done**: `containers.clear_project_cache_volume()`/`tools.tool_clear_project_cache()` exist and are tested, but nothing in `registry/` calls them, because no project-archival/removal flow exists anywhere in this codebase yet to call them from (`registry/registration.py` only ever adds projects; there's no `archive_project`/`remove_project` function of any kind). Wiring this in is deferred to whichever phase actually adds project archival/removal -- adding that flow itself is out of Phase 17's stated scope
- [x] Tests: unit tests (mocked Docker) asserting the cache volume is mounted with the right stack-specific bind path, the correct install command runs for each stack, and no install command runs when no manifest is present; a real-Docker integration test proving a `node`-stack project's `npm install` resolves a real package the Coder can subsequently `require()`/`import`

**Definition of Done:** a Python project and a Node project both round-trip through Coder -> dependency install -> `run_tests` using their own dedicated images, with `docker-socket-proxy`'s `BUILD` unchanged at `0` throughout.

**Verification status:** the Executor-layer changes are fully verified for real in this sandbox -- `pytest services/executor/tests/unit -q`: 65 passed (up from the pre-Phase-17 baseline), including 9 new tests covering per-stack cache-volume mount paths (`/home/sandbox/.cache/pip` for python, `/home/sandbox/.npm` for node, no second mount for an unregistered stack), install-command selection (`pip install -r requirements.txt`, falling back to `pip install -e .` when only `pyproject.toml` is present, `npm install`), and the no-manifest no-op case. `docker`/`pydantic`/`fastapi`/`httpx` are all importable here, so `test_api.py`'s `/clear_project_cache` and `stack`-threading tests are genuinely exercised too, not just hand-traced. The real-Docker integration test (`test_node_stack_run_command_installs_dependencies_from_package_json`, added to `tests/integration/test_executor_sandbox.py`) is written and skips cleanly here (no Docker daemon in this sandbox) but has **not run against a live daemon** -- run `pytest -m integration services/executor/tests/integration` on a Docker-enabled machine before treating the Definition of Done as met. The Orchestrator-side `stack` threading (`coder.py`, `tester.py`, `frontend_reviewer.py`, `graph/nodes.py`, `graph/state.py`) is verified for real everywhere it doesn't need `langgraph`/`langchain_core` -- `pytest services/orchestrator/agents/tests/unit/test_tester.py`: 16 passed, including the new `test_run_tests_uses_npm_framework_for_a_node_stack_project`. `test_coder.py`/`test_frontend_reviewer.py`/every `graph/tests/unit/*` fake was updated to accept the new `stack` kwarg and hand-traced against the real call sites, but is unverified by running (`langchain_core`/`langgraph` not installed in this sandbox, same pre-existing gap as every prior phase's graph layer). While validating this phase, `agents/__init__.py` was found eagerly re-exporting symbols from `coder.py`/`memory_curator.py`/`planner.py`, which transitively required `langchain_core` just to import LangChain-free submodules like `agents.tester` -- the same bug class Phase 6 already fixed once for `services/orchestrator/__init__.py`. Fixed by stripping `agents/__init__.py` to a docstring-only module (confirmed via `grep` that nothing imports from the package root); this is what unblocked `test_tester.py` running for real at all. Install `langgraph`/`langchain-core`/`langchain-anthropic`/`langchain-ollama` and run `pytest services/orchestrator -v` on a machine that has them before treating the graph-layer half of this phase as fully proven.

---

## Phase 18 — Multi-Project Concurrency

**Goal:** more than one project can be active at once without state bleed, with sane sharing of the GPU and the global cost budget.
**Decisions:** ADR-006

- [x] Confirm LangGraph thread isolation prevents state bleed between concurrent project threads (test: interleave two fixture projects through the graph, assert no cross-contamination of `task_queue` or `plan`)
- [x] Serialize local-LLM-bound work through a single inference queue/semaphore (GPU is shared); Claude-API-bound work runs in parallel without this constraint
- [x] Aggregate `cost_usd_weekly` across **all** active projects for the global cost threshold (the threshold is global, not per-project)
- [x] Tests: two "simultaneous" projects against a mocked local provider never issue overlapping calls to it; cost aggregation correctly sums across projects and trips the global threshold

**Definition of Done:** the interleaved two-project test shows no state bleed; the serialization test proves only one local-inference call is ever in flight.

**Verification status:** genuinely dependency-free suites ran for real in this sandbox -- `pytest services/orchestrator/graph/tests/unit/test_cost_aggregator.py services/orchestrator/tests/unit/test_config.py services/orchestrator/memory services/orchestrator/agents/tests/unit/test_tester.py -v`: 36 passed, 1 pre-existing unrelated `PytestCollectionWarning` about `TestOutcome`, in 5.94s. Breakdown: `test_cost_aggregator.py` 3/3 (the new global cost-aggregation helper, genuinely dependency-free), `test_config.py` 5/5 (including both new `global_cost_threshold_usd` tests), `memory` 12/12 and `agents/tests/unit/test_tester.py` 16/16 (both untouched by this phase, confirming no regression). `graph/tests/unit/test_state.py` could not run here -- `graph/state.py` imports `langchain_core.messages` at module level and `langchain_core` isn't installed in this sandbox -- but was hand-traced by its implementer and reviewed clean. Every other touched or added file this phase (`graph/__init__.py`, `graph/nodes.py`/`test_nodes.py`, `graph/build.py`/`test_graph_end_to_end.py`, `service.py`/`main.py`/`registry/cli.py`, `model_router/router.py`/`test_router.py`, `graph/tests/unit/test_concurrency.py`) needs `langgraph` and/or `langchain-anthropic`/`langchain-ollama`, none of which are installed here -- all were hand-traced against the real node/routing code and reviewed clean by a task reviewer, the same pre-existing gap as every graph-layer phase since Phase 5. Install `langgraph`, `langchain-core`, `langchain-anthropic`, and `langchain-ollama` and run `pytest --cov=services --cov-fail-under=80 --cov-report=term-missing` on a machine that has them before treating this phase as fully proven. Two real defects were found and fixed during this phase's own implementation: (1) wrapping `ChatOllama` in `_ThrottledChatModel` for GPU serialization initially broke a pre-existing test, `test_every_constructed_model_has_retries_enabled`, which accesses `.max_retries` directly on whatever `router.resolve(role)` returns -- fixed by adding an `__getattr__` passthrough to `_ThrottledChatModel` that delegates any attribute not explicitly wrapped to the underlying real model; (2) the thread-isolation test's own design (in the implementation plan, not shipped code) had two bugs caught before anything was committed -- it assumed each project's full 6-response script could be concatenated back-to-back when the test actually interleaves two projects' calls, and it seeded `initial_message` on both projects to assert on `messages[0].content`, which defeated `node_memory_curator`'s empty-`messages` no-op guard and caused two unscripted curator LLM calls per project mid-run. Both are corrected in the final test (`graph/tests/unit/test_concurrency.py`): no `initial_message` is seeded, scripted responses are split into per-project "start" and "resume" blocks in the true interleaved order, and isolation is proven via `plan.summary`/`project_id`/`phase` instead of `messages`.

---

## Phase 19 — Skills Packaging

**Goal:** every skill referenced above exists as a portable, independently-testable `SKILL.md`.
**Decisions:** ADR-014

- [x] Write `SKILL.md` for `develop-python`, `review-code`, `test-and-coverage`, `frontend-review`, `git-ops`, `docs-writer`, `roast-the-plan` — YAML frontmatter (`name`, `description`) + body, following Anthropic's Agent Skills format
- [x] A small eval per skill (a handful of input/expected-behavior fixture cases, runnable in isolation from the full orchestrator)
- [x] Verify each skill works unmodified whether the resolved provider is local or Claude API

**Definition of Done:** every skill has at least 2 passing eval cases; no skill file contains provider-specific instructions.

**Verification status:** `skills/_eval_lib`'s parsing/matching logic has real, executed pytest coverage: `pytest skills/_eval_lib/tests/unit -v --continue-on-collection-errors` → 12 passed, 3 failed, 1 error in 0.07s (`test_harness.py` 4/4 passed; `test_models.py` 7/7 passed, including the 3 media-type-sniffing tests added after Task 3's first review; `test_cli.py` 1/4 passed for real — `test_load_skill_body_strips_frontmatter`, pure string work — with the other 3 failing with `ModuleNotFoundError: No module named 'yaml'` raised from inside `cases_schema.py`'s `load_cases()`, since they call `run_skill_eval`; `test_cases_schema.py` fails at collection entirely with the same `ModuleNotFoundError` — its own module-level `import yaml`, used by its `_write_yaml` fixture-building helper, not by any production code — so none of its 6 tests execute in this combined run, though Task 1's review separately hand-traced all 6 as passing once pyyaml is installed). Running a bare `pytest skills/_eval_lib/tests/unit -v` without `--continue-on-collection-errors` aborts collection entirely on the `test_cases_schema.py` error and reports zero tests run — use the flagged invocation above. Every skill's `eval/cases.yaml` was validated for real via `cases_schema.load_cases()` (>=2 cases each, image paths resolved for frontend-review). No `ANTHROPIC_API_KEY` and no running Ollama instance are available in this sandbox, so `models.py`'s actual HTTP request/response handling and every skill's live-model eval run (the "passing" runs proving the skill prompts elicit correct behavior) could not be executed here -- run `python skills/<name>/eval/run_eval.py` with `ANTHROPIC_API_KEY` or `OLLAMA_BASE_URL` set on a real deployment before treating a skill's eval cases as proven, not just schema-valid. Provider-neutrality (no skill mentions a specific model/vendor) was confirmed by direct inspection of every `SKILL.md`, not by dual-provider execution. Two real defects were found and fixed during this phase's execution: (1) Task 1's `cases_schema.py` initially had a module-level `import yaml`, which would have forced pyyaml as a transitive dependency onto every downstream consumer (`harness.py`, `models.py`, `cli.py`) even though only `load_cases()` itself needs it — fixed by moving the import to be local inside `load_cases()`. (2) Task 3's `_anthropic_complete` initially hardcoded `media_type: "image/png"` regardless of actual image format — fixed by adding `_sniff_media_type()`, which detects PNG/JPEG via magic bytes and falls back to PNG (today's only real fixture).

---

## Phase 20 — Docs, README, CV Polish

**Goal:** a stranger can clone the repo and run it from the README alone; the project reads well on a GitHub profile.
**Decisions:** ADR-003

- [ ] Full `README.md`: pitch, embedded/linked architecture diagram, quickstart (`git clone` → `./scripts/start.sh` → first Telegram message), a screenshot or short clip of a real run if possible
- [ ] One human pass to confirm `ARCHITECTURE.md` still matches what was actually built
- [ ] `CONTRIBUTING.md` and a short `SECURITY.md` summarizing §8 of `ARCHITECTURE.md`
- [ ] Confirm the LICENSE choice before making the repo public
- [ ] Tag `v0.1.0` once Phases 0–19 are green

**Definition of Done:** someone who has never seen the project can read the README and get a running stack without asking a question.

---

## Phase 21 — Security Hardening Pass

**Goal:** deliberately try to break the boundaries described in `ARCHITECTURE.md` §8 before calling v1 done.
**Decisions:** ADR-005

- [ ] Attempt to read a path outside the bind mount from inside a workspace container — confirm it fails
- [ ] Attempt to reach `docker.sock` or a denied proxy endpoint (`BUILD`/`NETWORKS`/`VOLUMES`/`SWARM`/`SYSTEM`) directly — confirm denial
- [ ] Attempt a fork bomb / resource exhaustion inside a workspace container — confirm CPU/memory limits cap it without affecting the Executor itself
- [ ] Grep all logs from a full run for literal secret values — confirm none appear
- [ ] Write findings into `services/executor/SECURITY.md`, including anything that didn't hold up the first time and how it was fixed

**Definition of Done:** every attempted breakout fails as designed; the write-up exists regardless of outcome, including any accepted residual risk.

---

## Phase 22 — Stretch / Future Work

Not required for v1. Each item should get an `ARCHITECTURE.md` update (new ADR) before implementation starts.

- [ ] MCP servers (filesystem, GitHub) as an alternative or supplement to the custom Executor tools
- [ ] Neo4j-backed cross-project pattern memory, layered under — not replacing — the markdown files
- [ ] Prometheus `/metrics` + Grafana dashboard for the platform itself
- [ ] A2A Agent Card exposure, if Foundry ever needs to interoperate with another agent system -- verified against the real A2A spec (Linux-Foundation-governed as of mid-2025: HTTP/JSON-RPC/SSE transport, capability-discovery "Agent Cards", OAuth2/JWT mutual auth): this item is correctly scoped as *external* interop only. Foundry's own agents (Planner/Coder/Reviewer/Tester/Frontend Reviewer) are in-process LangGraph node functions sharing one `ProjectState` dict, not separate networked services with their own identity -- there is no internal "agent-to-agent" protocol to build, and there shouldn't be one until Foundry needs to talk to an agent system it doesn't own
- [ ] Workspace images for languages beyond Python and Node/npm (Phase 17), as real projects demand them -- Go is the natural next target (a single static-binary toolchain, no separate package-manager-install-on-startup complexity beyond `go mod download`)
- [ ] AMD 6750XT / ROCm-specific tuning for secondary agent roles
- [ ] Mid-implementation re-planning path: once a project passes `awaiting_plan_approval`, `plan`/`task_queue` are effectively frozen -- Reviewer and Tester can only send "fix this diff" feedback back to the Coder, never "the plan itself was wrong." If the Coder or Reviewer discovers a task rests on a false assumption partway through implementation, there is currently no route back to the Planner short of a human manually rejecting and restarting the whole project. Needs a `replan` phase/interrupt any implementation-side node can trigger
- [ ] Context management / compaction strategy: no phase addresses what happens when `messages`, `task_queue`, or a project's own file tree grow large enough to approach a provider's context window on a long-running or brownfield project -- every agent node currently reconstructs its prompt from full, uncompacted state on every call. Needs a documented summarization/compaction approach (even a simple one, e.g. curator-driven pruning of `messages`) before Foundry can be trusted on large or long-lived codebases
- [ ] Deployment-target tooling (Kubernetes, Docker Compose multi-service apps, etc.): the Coder's `run_command` action can invoke any binary present in the workspace image, but the only shipped image has no `kubectl`, no Docker CLI (deliberately -- ARCHITECTURE.md §4.3 and `.claude/rules/security.md` forbid Docker access outside the Executor itself), and no cluster-credentials story. A Coder agent with real `kubectl`/cluster access is a materially different trust boundary than one bind-mounted to a single project directory with no network egress and all capabilities dropped -- needs its own ADR and threat-model pass before any of this is added, not a workspace-image tweak
- [ ] Per-task structured progress tracking: `task_queue` items are free-form description strings with completion tracked only by removal from the list (no explicit pass/fail/blocked status per task). Anthropic's own published guidance on long-running coding-agent harnesses (see "Effective harnesses for long-running agents") specifically calls out a persistent, machine-readable feature/task list with per-item status as what prevents an agent from losing track of progress across a long session or a restart. Foundry already has the checkpointed `ProjectState` + per-project `PROJECT_MEMORY.md`/`LEARNINGS.md` (Phase 6) covering part of this, but no per-task status field a resumed session or a `/status` Telegram query could read directly
