# Foundry — Architecture

A self-hosted, Docker Compose–native multi-agent software development platform. Foundry plans, builds, reviews, and tests software autonomously, supervised through Telegram, running entirely on local hardware (RTX 3090 / 6750XT via Ollama or LM Studio) with optional Claude API for roles that need stronger reasoning, tool-calling reliability, or vision.

> Status: v1 built. Phases 0–20 of `TODO.md` are complete against this design (Phase 21's security hardening pass is next). This document is the source of truth for architecture decisions. Update it whenever a decision in §10 changes.

---

## Table of Contents

1. [Vision & Scope](#1-vision--scope)
2. [Design Principles](#2-design-principles)
3. [System Context](#3-system-context)
4. [Components](#4-components)
5. [State Schema](#5-state-schema)
6. [Sequence Diagrams](#6-sequence-diagrams)
7. [Docker Compose Topology](#7-docker-compose-topology)
8. [Security Model](#8-security-model)
9. [Repository Layout](#9-repository-layout)
10. [Decision Log](#10-decision-log)
11. [Out of Scope for v1 / Future Work](#11-out-of-scope-for-v1--future-work)
12. [Glossary](#12-glossary)

---

## 1. Vision & Scope

Foundry is both a daily-driver development tool and a portfolio piece. It should be the kind of thing that, on its own, demonstrates production-grade agent system design: clean separation of concerns, a real security boundary around code execution, human-in-the-loop control where it matters, and documentation good enough that a stranger could stand it up from the README.

**What Foundry does:**
- Takes a project idea (greenfield) or an existing repository (brownfield) and turns it into working, tested, reviewed code.
- Interrogates the plan before writing code — a structured intake interview followed by an adversarial self-critique pass ("roast the plan") — rather than guessing at ambiguous requirements.
- Runs autonomously through implement → test → review loops, with regular Telegram progress updates.
- Pauses for an explicit human decision only at the points that are genuinely hard to undo or have real cost: crossing a configured Claude API spend threshold, and merging into `main`.
- Supports multiple projects, each independently configured (coverage threshold, PR mode, cost limits).
- Runs entirely in containers. Nothing is installed on the host beyond Docker itself; agents never get a host shell.

**What Foundry is not (v1):** a SaaS product, a multi-tenant system, or a fully unattended CI replacement. It is a single-operator tool that happens to be built to a standard worth showing off.

---

## 2. Design Principles

Each principle below is grounded in a specific piece of prior art, not invented from scratch:

| Principle | Why | Inspired by |
|---|---|---|
| **Workspace is swappable, agent logic isn't** | Coding agents shouldn't care *where* code runs, only that a workspace can run commands and report output. Local-dev vs. containerized vs. (future) remote should be a config choice. | OpenHands v1's `Workspace` abstraction (Local/Docker/Remote) |
| **Append-only event log is the source of truth** | Every action, observation, and decision is an event. Replaying the log reconstructs full history — this is what makes memory, audit trails, and debugging tractable. | OpenHands v1 event log design |
| **Pause/resume is a first-class primitive, not a bolted-on feature** | Human approval shouldn't require polling or a separate state machine. The graph itself freezes and thaws. | LangGraph `interrupt()` / `Command(resume=...)` + checkpointer |
| **Capability-based model routing, not hardcoded model names** | Agent roles declare what they need (tool-calling, vision, context size); a router resolves that to whichever configured provider satisfies it. Swapping Ollama ↔ Claude API is a config change. | LiteLLM-style provider abstraction |
| **The LLM never touches infrastructure directly** | Agents call narrow, typed tools (`run_command`, `read_file`, `open_pr`). A separate trusted service is the only thing with real Docker/Git/GitHub credentials. | Docker's own findings on coding-agent sandboxing; standard least-privilege design |
| **Skills are portable, not framework-locked** | A "review code" or "review frontend" skill should work whether the executor is a local model, Claude API, or Claude Code. Write it once as a `SKILL.md`. | Anthropic's Agent Skills open standard |
| **Memory should be boring and inspectable** | A human (or a future agent) should be able to open a text file and understand what the system has learned. No opaque embeddings-only memory for v1. | Hermes Agent's session/skill persistence model; deliberately rejected Neo4j for v1 (see ADR-007) |

---

## 3. System Context

```mermaid
flowchart LR
    U["Mate (Telegram)"]

    subgraph FOUNDRY["Foundry - Docker Compose stack (on-demand)"]
        TG["Telegram Gateway"]
        ORCH["Orchestrator (LangGraph + FastAPI)"]
        EXE["Executor"]
        DSP["docker-socket-proxy (scoped API)"]
        MEM[("Memory: SQLite + Markdown")]
        REG[("Project Registry")]
    end

    WS["Ephemeral Workspace Containers (per task)"]
    DOCKER[("Host Docker Engine")]
    OLLAMA["Ollama / LM Studio (RTX 3090 / 6750XT)"]
    CLAUDE["Claude API"]
    GH["GitHub"]

    U <-->|messages, approvals| TG
    TG <--> ORCH
    ORCH <--> MEM
    ORCH <--> REG
    ORCH -->|tool calls| EXE
    EXE --> DSP
    DSP --> DOCKER
    DOCKER -.spawns.-> WS
    EXE <-->|exec, logs| WS
    ORCH -->|capability request| OLLAMA
    ORCH -->|capability request| CLAUDE
    ORCH -->|push, PR| GH
```

Everything inside the dashed boundary is a long-lived or on-demand Docker Compose service. Workspace containers are short-lived and spawned per task by the Executor — they are not part of the static compose file.

---

## 4. Components

### 4.1 Telegram Gateway

A thin, stateless bot service (`aiogram` or `python-telegram-bot`). Its only jobs:

- Authenticate the single allowed Telegram user ID (set during first-run setup).
- Forward free-text messages and commands (`/new`, `/status`, `/projects`, `/approve`, `/reject`) to the Orchestrator's HTTP API.
- Render Orchestrator notifications (progress updates, approval requests, plan drafts) as Telegram messages, using inline keyboards for approve/reject/edit decisions instead of asking the user to type free text where possible.
- Hold **no** business logic, memory, or credentials beyond its own bot token.

This is the only service that talks to the outside world on the human side. If Foundry ever needs to support a second messaging platform, only this service changes.

### 4.2 Orchestrator

A Python service running a LangGraph application, exposing a small FastAPI surface for the Gateway to call (`POST /webhook`, `POST /resume/{thread_id}`, `GET /projects`, `GET /status/{project_id}`).

Two graph levels live in the same process:

- **Global graph**: owns the project registry, routes incoming intents ("new project" vs. "continue project X" vs. "status check") to the correct project thread, aggregates cost across all projects, and is the only thing the Gateway talks to directly.
- **Per-project subgraph**: one LangGraph thread per active project (`thread_id = project_id`), holding that project's `ProjectState` (§5). This is the practical meaning of "sub-orchestrator per active project" — logical isolation via separate threads and state, not a separate OS process per project. If a project later needs harder isolation (e.g. conflicting Python environments at the orchestration layer itself, not just the workspace), promoting it to its own process is a config change, not a rewrite — the LangGraph app is structured so a subgraph can be lifted into its own service.

A Postgres or SQLite checkpointer (start with SQLite given the single-operator scope; see ADR-008) persists graph state so `interrupt()` survives restarts — you can `./foundry down`, come back tomorrow, and resume exactly where a pending approval left off.

### 4.3 Executor & Docker Security Boundary

The Executor is the **only** service that can cause a container to be created, started, or killed. It exposes a narrow internal API to the Orchestrator:

```
run_command(project_id, command, timeout) -> {stdout, stderr, exit_code}
read_file(project_id, path) -> content
write_file(project_id, path, content) -> ok
list_files(project_id, path) -> [entries]
run_tests(project_id, framework) -> {passed, failed, coverage_pct, report}
```

No tool gives an agent raw Docker API access or a host shell. The Orchestrator's agents only ever see these typed functions.

Underneath, the Executor talks to `tecnativa/docker-socket-proxy`, **not** the raw `/var/run/docker.sock`. The proxy is configured to allow only what's needed and deny everything else:

```yaml
# docker-socket-proxy environment (illustrative)
CONTAINERS: 1   # create/start/stop/inspect/remove
EXEC: 1         # exec into running containers
IMAGES: 1       # read image list / pull
POST: 1         # allow the above to actually mutate state
BUILD: 0        # no remote builds via the proxy (build workspace images via a separate, explicit step)
NETWORKS: 0     # no host network reconfiguration
VOLUMES: 0      # no arbitrary volume creation - Executor code decides mounts, never the LLM
SWARM: 0
SYSTEM: 0
```

This is the deliberate alternative to either (a) mounting the real socket directly into a service an LLM's tool calls can reach, or (b) privileged Docker-in-Docker. See ADR-005 for the full reasoning and the upgrade path to Docker's microVM-based Sandboxes if/when that becomes stable enough for a Compose-native workflow.

Each task gets a fresh **workspace container**:
- Built from a per-language base image, selected by the project's `stack` (`foundry/workspace-python:3.12` for v1; `foundry/workspace-node` from Phase 17 onward — see ADR-028). No agent-reachable code path may build or customize an image; only a fixed, operator-maintained set of pre-built images exists.
- The target project's directory bind-mounted, read-write, plus (from Phase 17) one Docker-managed named volume per project holding that project's package-manager cache (`~/.cache/pip`, `~/.npm`) — nothing else from the host, and never a second host path.
- Runs as a non-root user, `--cap-drop=ALL`, `--security-opt=no-new-privileges`.
- Attached to an isolated bridge network (`foundry_workspace`), separate from the core services network and never `host` networking. No egress allowlist is enforced yet — the eventual goal is restricting outbound traffic to package registries only, but that needs a forward-proxy or NAT/iptables component that isn't built; see `services/executor/SECURITY.md`'s accepted residual risk.
- CPU/memory limits set per project (sane defaults, overridable in `projects.yaml`).
- Torn down after the task completes; nothing persists in the container itself, and no image is ever built or modified — anything that should persist goes through `write_file` into the bind-mounted project directory, and the only thing that persists in the cache volume is downloaded packages, never application state or credentials.

Because bind-mount sources are resolved by the Docker daemon against the *host* filesystem regardless of which container asked for the mount, the Executor (which does not have `projects/` mounted into its own container — only the Orchestrator does) needs the host's absolute path to `projects/` to construct correct mounts. This is `PROJECTS_HOST_PATH`, an operator-set environment variable read once at Executor startup — see `services/executor/SECURITY.md` for the full Docker-outside-of-Docker explanation.

As of Phase 22 (ADR-045), the per-language workspace Dockerfiles are no longer hand-maintained: each stack (python, node, go) is declared once in `config/stacks.yaml` (base image, apt packages, manifest filenames, install command, cache directory, static-check commands) and rendered from a single shared `images/workspace.template.Dockerfile` by an operator-run `scripts/gen_workspace_images.py` — the committed Dockerfiles are generator output, not hand-edited, and a golden test locks them to what the template actually produces so hardening drift between stacks (the exact problem that motivated the template) shows up as a failing test rather than a silent diff. `image_for_stack()`, install-command selection, and cache paths in the Executor, plus `stack` validation in the registry, all read this same YAML now instead of hardcoded per-stack dicts and `Literal`s.

### 4.4 Model Router

A library (not a network service — no reason to pay a network hop on a single-host system) imported by every agent node. Configuration lives in `config/models.yaml`. Providers are named *instances*, not provider *types* — a single `type: anthropic` provider can be declared multiple times under different names to expose distinct Claude model tiers (e.g. Opus for planning/critique, Sonnet for coding, Haiku for cheap curation), so per-role cost/capability tuning doesn't force every role onto the same pinned model string:

```yaml
roles:
  planner:
    requires: [tool_calling, long_context]
    prefer: [claude_opus, local]
  coder:
    requires: [tool_calling]
    prefer: [local, claude_sonnet]
  reviewer:
    requires: [tool_calling]
    prefer: [local, claude_sonnet]
  frontend_reviewer:
    requires: [tool_calling, vision]
    prefer: [claude_sonnet]      # local models lack reliable vision; documented, not hidden
  memory_curator:
    requires: []
    prefer: [local, claude_haiku]

providers:
  local:
    type: ollama
    base_url: http://ollama:11434
    model: qwen3-coder:30b-a3b
    capabilities: [tool_calling]
    cost_per_1m_input_tokens: 0.0
    cost_per_1m_output_tokens: 0.0
  claude_opus:
    type: anthropic
    model: claude-opus-4-8
    capabilities: [tool_calling, long_context, vision]
    cost_per_1m_input_tokens: 15.00
    cost_per_1m_output_tokens: 75.00
  claude_sonnet:
    type: anthropic
    model: claude-sonnet-5
    capabilities: [tool_calling, long_context, vision]
    cost_per_1m_input_tokens: 3.00
    cost_per_1m_output_tokens: 15.00
  claude_haiku:
    type: anthropic
    model: claude-haiku-4-5
    capabilities: [tool_calling]
    cost_per_1m_input_tokens: 0.80
    cost_per_1m_output_tokens: 4.00
```

Each provider declares its own `capabilities:` list explicitly (ADR-016) rather than the router inferring capabilities from `type`. This keeps capability-based routing fully config-driven: if a future Claude tier gains or lacks a capability, that's a `models.yaml` edit, not a code change.

The router resolves `role -> provider` at call time using `prefer` order and falls back to the next provider if the preferred one is unavailable. Availability of `local` is resolved **once at process startup** — from whether the `local-llm` Compose profile is active (e.g. an `OLLAMA_ENABLED` env var set by `start.sh` vs. `start.sh --local-llm`) — not by probing `http://ollama:11434` on every call. This matters because several roles above list `local` first: without a startup-time check, a Claude-only deployment (no `local-llm` profile started) would pay a connection-timeout tax on every single `coder`/`reviewer`/`memory_curator` invocation just because `local` happens to be preferred.

**Claude-only is a first-class deployment mode, not a derived fallback state.** `config/models.claude-only.yaml` ships as a documented preset where every role's `prefer` list contains only `claude_*` providers and no `local` entry at all — the config an operator without a GPU (or who simply wants Claude's reliability everywhere) drops in unchanged, rather than something they have to hand-derive by reordering the local-first default. Switching between presets, or hand-editing `prefer` order, is a config change, not a code change.

**`resolve(role)` returns a LangChain `BaseChatModel`, not a custom `Provider` object (ADR-025, Phase 15).** The router constructs a `ChatAnthropic` or `ChatOllama` instance per configured provider entry (still gated by the same capability/`prefer`-chain logic described above) instead of a hand-rolled class implementing a custom `complete()` method. Agent code calls `.ainvoke()`/`.astream()`/`.bind_tools()`/`.with_structured_output()` directly on the resolved model — native tool-calling, retry/backoff (`max_retries` set at construction), and streaming all come from LangChain's maintained integrations rather than code Foundry owns. `cost_per_1m_input_tokens`/`cost_per_1m_output_tokens` per provider (shown above) feed a small helper that turns a response's `usage_metadata` into a dollar figure accumulated into `ProjectState.cost_usd_session`/`cost_usd_weekly` (ADR-026) — this is what makes the cost-threshold gate (§10 ADR-009) actually trip from real usage. The role/provider/capability model in `config/models.yaml` is unchanged; only what `resolve()` hands back to the caller changed. Every Ollama-backed model is additionally wrapped in a small internal throttle (`_ThrottledChatModel`) sharing one `threading.Semaphore(1)` per router instance (Phase 18) -- one physical GPU serves every locally-routed role in a deployment, so local calls are serialized while Claude-API-bound calls run in parallel.

### 4.5 Memory System

Two layers, deliberately kept simple (ADR-007):

**Per-project (git-tracked, human-readable):**
```
projects/<project_id>/memory/
  PROJECT_MEMORY.md   # what this project is, current state, key constraints
  DECISIONS.md        # append-only log of decisions made during planning/build
  LEARNINGS.md        # things that went wrong and how they were fixed, this project only
```

**Global (SQLite, cross-project recall):**
```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY,
    project_id TEXT,
    type TEXT,        -- 'error_pattern' | 'fix' | 'preference' | 'skill_usage'
    content TEXT,
    tags TEXT,
    created_at TEXT
);
CREATE VIRTUAL TABLE memories_fts USING fts5(content, tags, content=memories);
```

A **Memory Curator** agent runs after each completed phase (not after every message — that would be noisy and expensive). It reads the raw event log for the phase, writes/updates the project's markdown files, and inserts any cross-project-relevant lesson into the global SQLite store. This directly mirrors Hermes Agent's curator pattern, simplified to fit a single-operator system.

Full-text recall uses SQLite FTS5 — proven at this scale (Hermes uses the same mechanism for session search) — no vector database needed for v1.

### 4.6 Project Registry

`projects.yaml` (or a `projects` table in the same SQLite file as memory — start with YAML for human-editability, migrate to SQLite only if the file gets unwieldy):

```yaml
- id: foundry-itself
  name: Foundry
  mode: greenfield
  path: /data/projects/foundry-itself
  git_remote: git@github.com:matete/foundry.git
  stack: python
  coverage_threshold: 80
  pr_mode: configurable    # auto | manual | configurable
  coverage_gate_mode: hard # hard | soft | configurable -- Tester's enforcement strictness (ADR-023), independent of pr_mode
  cost_ceiling_usd_weekly: null   # null = use global default
  status: active
```

### 4.7 Specialist Agents (LangGraph nodes)

| Agent | Responsibility | Preferred model capability |
|---|---|---|
| **Planner / Intake** | Structured interview, then a self-adversarial "roast the plan" critique pass before presenting a final plan for approval. | tool-calling, long context |
| **Coder** | Implements tasks inside a workspace container via the Executor. | tool-calling |
| **Static Checks** | Deterministic lint/typecheck/build commands from `stacks.yaml`, run between Coder and Reviewer (ADR-046) — failure loops back to Coder with the raw tool output as feedback, never spending a Reviewer token on output a linter would have caught for free. | none required — deterministic code (`agents/static_checks.py`), no LLM role in `config/models.yaml` |
| **Reviewer** | Reviews diffs for correctness, style, security smells before tests are even run. | tool-calling |
| **Tester** | Runs the test suite, computes coverage, compares to the project's configured threshold. | none required — deterministic code (`agents/tester.py`), no LLM role in `config/models.yaml` |
| **Frontend Reviewer** | Visual review of UI changes (screenshots via Playwright). | vision — routes to Claude by default per §4.4 |
| **Docs Writer** | Keeps README/architecture docs in sync with what was actually built. | Not yet a wired LangGraph node — ships as a standalone `skills/docs-writer/SKILL.md` (Phase 19) invoked manually, no `docs_writer` role in `config/models.yaml` |
| **Git/GitHub Integrator** | Commits to feature branches, opens PRs (if `pr_mode` allows), never touches `main` without approval. | none required — deterministic code (`IntegratorClient`, invoked from `node_done`), no LLM role in `config/models.yaml` |
| **Memory Curator** | Distills session events into project + global memory after each phase. | none required |

### 4.8 Skills

Packaged as portable `SKILL.md` folders (Anthropic's open Agent Skills standard), so they work identically whether the executing model is local, Claude API, or — if ever needed — Claude Code directly:

```
skills/
  develop-python/SKILL.md
  review-code/SKILL.md
  test-and-coverage/SKILL.md
  frontend-review/SKILL.md
  git-ops/SKILL.md
  docs-writer/SKILL.md
  roast-the-plan/SKILL.md
```

Each skill's `eval/` directory holds `cases.yaml` (>=2 fixture cases, each planting one concrete, checkable issue) and a `run_eval.py` entrypoint; both call a shared harness (`skills/_eval_lib/`) whose `CompleteFn` injection point keeps its parsing/matching logic testable without a live model while the harness itself stays fully decoupled from `ModelRouter` (ADR-033).

---

## 5. State Schema

```python
class ProjectState(TypedDict):
    project_id: str
    mode: Literal["greenfield", "brownfield"]
    stack: str
    phase: Literal[
        "intake", "planning", "roasting", "awaiting_plan_approval",
        "implementing", "reviewing", "testing", "awaiting_cost_approval",
        "awaiting_merge_approval", "awaiting_replan_approval", "pr_pending",
        "done", "blocked",
    ]
    plan: dict
    messages: Annotated[list[BaseMessage], add]
    task_queue: list[dict]
    cost_usd_session: float
    cost_usd_weekly: float
    coverage_pct: float | None
    coverage_threshold: int
    pr_mode: Literal["auto", "manual", "configurable"]
    coverage_gate_mode: Literal["hard", "soft", "configurable"]
    git_branch: str | None
    git_remote_url: str | None
    curator_source: str | None
    replan_count: int
    pending_approval: dict | None
```

`stack` is a plain `str`, not a closed `Literal` — Phase 22 widened it so the registry can grow without a code change; the actual set of valid values is whatever keys `config/stacks.yaml` defines, enforced at the registry CLI boundary rather than by the type checker.

`pending_approval` is what gets attached to the `interrupt()` payload — the Gateway reads it to render the right Telegram message (plan text + approve/reject buttons, or "cost threshold reached: $42.10 of $50/week — continue?").

Each `task_queue` item is `{"description": str, "status": "pending" | "in_progress" | "done" | "blocked", "attempts": int}` (ADR-034). Every task for the current implementation phase stays in the list for its lifetime — nothing is removed on completion — so a resumed session or a `/status` query can see the full per-task history, not just whichever task is currently active. There is no separate `current_task` field: the active task (at most one at a time) is whichever item has `status == "in_progress"`.

`replan_count` (ADR-047) counts how many times the human has approved a redraft after a task exhausted `MAX_TASK_ATTEMPTS` or the Reviewer flagged the plan itself as broken (`replan_needed`); it starts at 0 and is incremented only by `node_replan_wait` on approval, never elsewhere. Once it reaches `MAX_REPLANS` (2), the same trigger that would have armed `replan_wait` instead blocks the project outright — `awaiting_replan_approval` and its `replan_approval`-kind `pending_approval` are never produced at the cap.

---

## 6. Sequence Diagrams

### 6.1 New Project Intake ("Roast the Plan")

```mermaid
sequenceDiagram
    participant U as Mate (Telegram)
    participant TG as Telegram Gateway
    participant O as Orchestrator
    participant P as Planner Agent

    U->>TG: "/new - build X that does Y"
    TG->>O: POST /webhook
    O->>P: start intake(thread_id=new)
    loop structured interview
        P->>O: clarifying question
        O->>TG: question
        TG->>U: question (buttons where possible)
        U->>TG: answer
        TG->>O: answer
        O->>P: answer
    end
    P->>P: draft PLAN.md
    P->>P: self-critique pass ("roast the plan")
    P->>O: final plan + critique notes
    O->>TG: plan summary + [Approve] [Edit] [Reject]
    TG->>U: render plan
    U->>TG: Approve
    TG->>O: Command(resume="approve")
    O->>O: phase -> implementing, register project
```

### 6.2 Implementation Loop with HITL Gates

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant C as Coder
    participant EXE as Executor
    participant R as Reviewer
    participant T as Tester
    participant TG as Telegram Gateway
    participant U as Mate

    O->>C: next task
    C->>EXE: run_command(...) / write_file(...)
    EXE->>EXE: spawn ephemeral workspace container
    EXE-->>C: stdout/stderr/exit_code
    C->>O: diff ready
    O->>O: static checks (deterministic, ADR-046) -- failure loops back to C
    O->>R: review diff
    R->>O: comments / approved
    O->>T: run_tests()
    T->>EXE: run_tests(project_id)
    EXE-->>T: coverage result
    alt coverage below threshold (hard gate)
        T->>O: blocked
        O->>C: fix coverage gap
    else coverage OK
        O->>TG: progress update (FYI, no approval needed)
        TG->>U: "Task 4/9 done, coverage 84%"
    end
    Note over O: cost_usd_session crosses configured threshold
    O->>O: interrupt(payload=cost_alert)
    O->>TG: "Crossed $50/week - continue?" [Approve] [Reject] [Edit]
    TG->>U: render approval
    U->>TG: Approve
    TG->>O: Command(resume="approve")
```

Every interrupt gate in the per-project subgraph -- cost, merge-to-main,
the Planner's plan-approval gate (§6.1), and the replan gate (`replan_wait`,
ADR-047) -- shares the same "approve"/"reject"/"edit" decision vocabulary
and the same generic Telegram renderer (one Approve/Reject/Edit inline
keyboard), rather than each gate defining its own button labels/values.
"Edit" isn't handled as a distinct revision path yet in any gate; today it's
treated the same as "reject". `replan_wait` sits between a task exhausting
`MAX_TASK_ATTEMPTS` (or the Reviewer setting `replan_needed`) and the
Planner: approval routes back to `planner_draft` with the failure context
injected into `messages` and every already-`done` task preserved, so the
redrafted plan goes through the ordinary plan-approval gate again rather
than being applied unilaterally; rejection halts exactly like every other
reject path. It is bypassed once `replan_count` reaches `MAX_REPLANS` --
the triggering condition blocks the project directly instead.

### 6.3 Merge-to-Main Gate

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant G as Git/GitHub Integrator
    participant TG as Telegram Gateway
    participant U as Mate

    G->>O: feature branch ready, all tests green
    alt pr_mode == auto
        G->>G: open PR via GitHub API
        O->>TG: "PR #12 opened, ready for your review on GitHub"
    else pr_mode == manual
        O->>TG: "Branch pushed: feature/x - open the PR yourself when ready"
    end
    Note over O: merge into main always pauses, regardless of pr_mode
    O->>O: interrupt(payload=merge_request)
    O->>TG: "Ready to merge PR #12 into main. [Approve] [Hold]"
    TG->>U: render approval
    U->>TG: Approve
    TG->>O: Command(resume="approve")
    O->>G: merge
```

---

## 7. Docker Compose Topology

```mermaid
flowchart TB
    subgraph profile_core["profile: core (always)"]
        TG["telegram-gateway"]
        ORCH["orchestrator"]
        EXE["executor"]
        DSP["docker-socket-proxy"]
    end
    subgraph profile_local["profile: local-llm (optional)"]
        OLLAMA["ollama (GPU: RTX 3090 passthrough)"]
    end
    VOL1[("volume: foundry_memory.db")]
    VOL2[("volume: projects/")]
    VOL3[("volume: ollama_models")]
    SOCK[("/var/run/docker.sock - host, mounted into proxy only")]

    TG --- ORCH
    ORCH --- VOL1
    ORCH --- VOL2
    ORCH --- EXE
    EXE --- DSP
    DSP --- SOCK
    OLLAMA --- VOL3
    ORCH -.optional capability call.-> OLLAMA
```

Started on demand via `./foundry up` (not 24/7 — see ADR-010). `docker-socket-proxy` is the *only* container that mounts the real host socket; every other service, including Executor, only ever reaches the proxy's HTTP API over the internal Compose network.

---

## 8. Security Model

1. **No host execution.** Agents never get a shell on the host. The only host-side artifact is the Docker Compose stack itself and the bind-mounted project directories under `projects/`.
2. **Docker socket is never directly exposed to agent-reachable code.** `docker-socket-proxy` sits between the real socket and the Executor, with `BUILD`, `NETWORKS`, `VOLUMES`, `SWARM`, and `SYSTEM` denied (§4.3). Mount decisions are made by Executor code, never by an LLM tool call.
3. **Workspace containers are least-privilege**: non-root, `cap-drop=ALL`, `no-new-privileges`, isolated network (no egress allowlist enforced yet — accepted residual risk, see `services/executor/SECURITY.md`), CPU/memory limits, and torn down after each task.
4. **Secrets via Docker Compose secrets**, populated once by `./scripts/setup.sh` on first run (Telegram bot token, Claude API key, GitHub token). Never logged, never interpolated directly into prompts — referenced by name and injected only at the tool layer that needs them.
5. **Single-operator authentication.** The Telegram Gateway only responds to one allow-listed Telegram user ID, set during setup.
6. **Hard-to-undo actions are gated.** Per the decision log: merging into `main` always pauses for approval. Everything else (feature-branch commits, file deletes within the project mount, dependency installs inside workspace containers) runs autonomously, with the Claude API weekly cost threshold as the other explicit gate.

---

## 9. Repository Layout

```
foundry/
├── ARCHITECTURE.md
├── TODO.md
├── README.md
├── docker-compose.yml
├── docker-compose.local-llm.yml      # profile overlay
├── .env.example
├── secrets/                          # gitignored - only *.example committed
├── scripts/
│   ├── setup.sh                      # first-run secrets + config wizard
│   ├── start.sh
│   └── stop.sh
├── config/
│   └── models.yaml
├── images/
│   ├── workspace-python/Dockerfile
│   └── ...
├── services/
│   ├── telegram_gateway/
│   ├── orchestrator/
│   │   ├── graph/                    # LangGraph nodes, state, checkpointer
│   │   ├── agents/                   # planner, coder, reviewer, tester, ...
│   │   └── memory/
│   └── executor/
├── skills/
│   ├── develop-python/SKILL.md
│   ├── review-code/SKILL.md
│   ├── test-and-coverage/SKILL.md
│   ├── frontend-review/SKILL.md
│   ├── git-ops/SKILL.md
│   ├── docs-writer/SKILL.md
│   └── roast-the-plan/SKILL.md
├── projects/                         # bind-mounted project workspaces + memory
└── tests/
```

---

## 10. Decision Log

Lightweight ADR-style record of every decision made during planning, so the reasoning isn't lost.

| # | Decision | Rationale |
|---|---|---|
| 001 | Support both local (Ollama/LM Studio) and Claude API, swappable per role | Cost control + capability flexibility; capability-based router (§4.4) keeps the swap config-only |
| 002 | Telegram as the sole messaging interface | Simplicity for a single-operator system; Gateway is isolated so a second platform is a contained addition later |
| 003 | Build to portfolio quality from day one | Explicit goal — clean docs, diagrams, ADRs, tests throughout, not bolted on at the end |
| 004 | Secrets via Docker Compose secrets, wizard on first run | Compose-native, no extra service, good enough for single-host/single-operator |
| 005 | Docker access via `docker-socket-proxy`, never a raw socket mount into agent-reachable services | Mounting `docker.sock` directly grants root-equivalent host access; a scoped proxy is the standard mitigation short of microVM isolation (Docker Sandboxes), which isn't yet Compose-native enough to adopt for v1 |
| 006 | Global orchestrator + per-project LangGraph thread (not per-project OS process) | Matches "sub-orchestrator per project" intent without N processes; promotable to separate processes later if isolation needs grow |
| 007 | Memory = git-tracked markdown (per-project) + SQLite/FTS5 (global), no Neo4j or vector DB in v1 | Explicitly chosen over a Neo4j hybrid: keeps memory human-readable and git-diffable; revisit if cross-project pattern queries become a real bottleneck (see §11) |
| 008 | SQLite checkpointer for LangGraph persistence (not Postgres) | Matches single-operator, on-demand-startup scope; Postgres is a one-line swap if ever needed |
| 009 | Only two hard approval gates: Claude API cost threshold (globally configurable) and merge-to-`main` | User explicitly chose minimal gating for autonomy; merge-to-main added as a non-negotiable safety net regardless of cost state, since it's the one classically hard-to-undo action |
| 010 | Stack starts on-demand (`./foundry up`), not 24/7 | Home GPU box; avoids idle power/thermal/noise cost and unnecessary always-on attack surface |
| 011 | No Prometheus/Grafana in v1 | Explicitly deferred - structured logs only for now; clean upgrade path exists if added later (see §11) |
| 012 | Python-first workspace images | Matches primary stack; polyglot images added incrementally as needed (Phase pattern in TODO.md) |
| 013 | Coverage threshold and PR mode configurable per project | Not every project needs the same bar; registry (§4.6) carries per-project overrides |
| 014 | Skills authored as portable `SKILL.md` | Open standard, works across local/Claude API/Claude Code without rewriting |
| 015 | Claude-only is a first-class model-router deployment mode; Claude providers are declared per-tier (`claude_opus`/`claude_sonnet`/`claude_haiku`), and `local` availability is a startup-time check, not a per-call probe | An all-Claude deployment (no GPU, or an operator who just wants Claude everywhere) must not pay Ollama connection-timeout latency on every call to a `local`-preferring role, and should be able to route different roles to different Claude cost/capability tiers instead of one pinned model for every role |
| 016 | Provider capabilities (`tool_calling`, `long_context`, `vision`, ...) are declared explicitly per provider entry in `models.yaml` via a `capabilities:` field, not inferred from `type` | Keeps capability-based routing (§4.4) fully config-driven — a provider's real capability profile can change (e.g. a future Claude tier gaining or lacking vision) without a code change; inferring capabilities from `type` would hardcode an assumption that every provider of a given type shares identical capabilities |
| 017 | The "global graph" (§4.2) is implemented as a plain Python routing layer (`services/orchestrator/service.py`) over the compiled per-project subgraph, not as a second nested `StateGraph` | Its Phase 5 responsibilities — routing a webhook to a thread by `project_id`, reading status, listing known projects — are single-step dict/thread-id lookups with no branching or `interrupt()`/resume needs of their own; only the per-project subgraph needs LangGraph's state-machine machinery. Revisit if Phase 7's registry or cross-project cost aggregation later needs multi-step graph semantics at this level — the FastAPI contract and `ProjectState` schema wouldn't need to change either way |
| 018 | `projects.yaml` (§4.6) lives at `/data/projects/projects.yaml`, on the same bind-mounted volume as the project workspaces themselves, not under `config/` | §4.6 didn't pin down a path. `.gitignore` already carved out exactly this shape before Phase 7 existed (`projects/*` ignored, `projects/*/` and `projects/*/memory/*.md` un-ignored) — a file directly under `projects/` is runtime/generated state, consistent with treating it as part of the same volume rather than hand-edited, git-committed config like `config/models.yaml` |
| 019 | LLM-backed graph nodes (the Planner; future Coder/Reviewer/Tester) stay synchronous `def` functions that bridge to `async` agent-layer calls via `asyncio.run()`, rather than making the whole per-project subgraph `async` | Mixing one `async def` node into a graph forces every caller to switch from `.invoke()`/`.get_state()` to `.ainvoke()`/`.aget_state()` — which would have meant reworking `OrchestratorService` and the FastAPI layer (Phase 5) just to accommodate the Planner. A sync node that internally awaits its own event loop via `asyncio.run()` keeps every other node, `OrchestratorService`, and the FastAPI layer unchanged; the tradeoff is one node running one LLM call at a time instead of concurrently, which is a non-issue at this system's single-operator, single-active-thread-per-project scale. Revisit if a future phase genuinely needs concurrent LLM calls within one node |
| 020 | Every human-in-the-loop gate (cost, merge-to-main, plan-questions, plan-approval) shares one decision vocabulary -- "approve"/"reject"/"edit" -- and one generic Telegram render path, instead of gate-specific button text | Phase 5's original cost-gate stub used "continue"/"pause" (matching §6.2's original sequence diagram text) while the merge gate used "approve"/"reject"; Phase 4's Telegram Gateway, built independently, only ever renders one generic Approve/Reject/Edit keyboard (`services/telegram_gateway/rendering.py`) regardless of the interrupt's `kind`. That mismatch meant a real cost-gate approval tap would have been silently misread as a rejection. Standardizing on the vocabulary Telegram's renderer actually sends was the fix, applied everywhere rather than teaching the renderer per-kind button sets |
| 021 | The Executor gets a real FastAPI HTTP surface (`services/executor/api.py`), and `services/orchestrator/executor_client.py` defines its own response models rather than importing `services.executor.schemas` | §4.3 always described a "narrow internal API" between two separately-deployed containers, but through Phase 8 only Python-level `tool_*` functions existed -- nothing reachable over the network. Phase 9's Coder is the first thing that actually needs the Orchestrator to call the Executor, so the HTTP surface had to exist for real. Each service keeps its own Pydantic response models (matching the existing Telegram-Gateway/Orchestrator relationship) so neither service's deployment depends on the other's internal module layout, and the Orchestrator never needs the Docker SDK as a transitive dependency at all |
| 022 | The Coder drives a manual ReAct loop (one LLM call decides one action, dispatched by hand to `read_file`/`write_file`/`run_command`, the result fed back as the next turn's context) instead of relying on a provider's native tool-calling | `model_router`'s `CompletionRequest`/`CompletionResult` (§4.4) is a plain text-completion contract with no function-calling fields -- adding one would mean teaching every provider implementation a tool-call wire format before any agent needed it. The Coder's loop reuses the same single-call, structured-text-response shape the Planner already established (ADR groundwork from Phase 8), parsed with a shared helper (`services/orchestrator/agents/parsing.py`) |
| 023 | `ProjectState` and the project registry (§4.6) get their own `coverage_gate_mode: "hard" \| "soft" \| "configurable"` field, separate from `pr_mode`, defaulting to `"hard"` | TODO.md Phase 11 required all three Tester coverage-gate modes, defaulting new projects to hard -- but a first implementation took a shortcut and derived the gate mode from `pr_mode` (`auto`→hard, `manual`→soft, `configurable`→configurable) instead of adding a field, since `pr_mode` was the only "mode"-shaped per-project setting already in the schema. That conflated two unrelated concerns (`pr_mode` governs GitHub PR automation, §4.7's Git/GitHub Integrator; coverage strictness has nothing to do with it) and silently violated the "default to hard" requirement, since new projects default `pr_mode` to `"manual"`, which mapped to `"soft"`. Caught in a Phase 11 validation pass; fixed by giving coverage strictness its own field rather than overloading an unrelated one |
| 024 | `tool_take_screenshot` renders pages with real headless Chromium via Playwright (`images/workspace-python/Dockerfile` installs `playwright` + `chromium` to a world-readable `PLAYWRIGHT_BROWSERS_PATH`, launched with `--no-sandbox --disable-dev-shm-usage` since the workspace container's `cap_drop=["ALL"]`/non-root/no-new-privileges constraints (`.claude/rules/security.md`) rule out Chromium's own setuid sandbox) | §4.7 always specified "screenshots via Playwright" for the Frontend Reviewer, but a first Phase 12 implementation swapped this for urllib + Pillow ("no Playwright dependency required") without updating this doc to match. That approach never actually rendered anything -- it fetched the target URL's raw HTTP response bytes and handed them to `PIL.Image.open()`, which can only decode a response that's already an image file; for any real HTML/CSS/JS page it always raised `PIL.UnidentifiedImageError`. Caught in a Phase 12 validation pass alongside two smaller bugs in the same code path (the vision system prompt was built but never attached to the request sent to the model, and `take_screenshot()` was never called with `auto_start_server=True` even though every call runs in a fresh, ephemeral container with nothing already listening) |
| 025 | `model_router.resolve(role)` returns a LangChain `BaseChatModel` (`ChatAnthropic`/`ChatOllama`), replacing the hand-rolled `Provider` protocol (`services/orchestrator/model_router/provider.py`) and its custom `AnthropicProvider`/`OllamaProvider` implementations (Phase 15) | ADR-022 deliberately chose a plain text-completion contract with no function-calling fields for v1, reasoning that no agent yet needed more than one structured action per turn. By Phase 12 that was no longer true across the board: Planner/Reviewer/Frontend Reviewer all ask the model to hand-write "## Verdict" / "## Findings" markdown that's then parsed with regex-and-string-split helpers (`agents/parsing.py`) prone to silently dropping malformed bullets, and the Coder's manual ReAct loop has the same fragility for action selection. LangChain's `BaseChatModel` gives every role native tool-calling (`bind_tools`), schema-forced structured output (`with_structured_output`, replacing `agents/parsing.py` outright), streaming (`astream`), and retry/backoff (`max_retries`) from a maintained integration instead of code Foundry has to keep correct itself -- at the cost of a real migration across every already-built agent. The role/provider/capability config model in `config/models.yaml` (ADR-015, ADR-016) is unchanged; only what `resolve()` constructs and returns changes |
| 026 | Provider $ pricing lives in `config/models.yaml` (`cost_per_1m_input_tokens`/`cost_per_1m_output_tokens` per provider entry), and a small helper turns each LLM call's `usage_metadata` into a dollar delta accumulated into `ProjectState.cost_usd_session`/`cost_usd_weekly` (Phase 15) | `TokenUsage` (input/output token counts) was captured on every Claude API response from early on, but nothing ever converted it to a dollar amount or wrote it into the cost fields the cost-threshold gate (ADR-009) reads -- the gate could only ever trip via a manually-set test value, never from real usage. Keeping prices in config rather than a third-party pricing-lookup package (e.g. litellm's cost calculator, considered and rejected) matches this repo's existing rule that model specifics live in `config/models.yaml`, not code, and avoids a dependency whose primary purpose (model routing/calling) this project already has its own layer for |
| 027 | Live progress reaches Telegram via the Gateway polling a new Orchestrator `GET /stream/{project_id}` endpoint, not the Orchestrator pushing over SSE/websocket (Phase 16) | §11 already flagged that no proactive Orchestrator -> Gateway notification path exists at all -- every gate, block, and now streamed-progress update needed one. A poller reuses the same FastAPI/aiogram stack both services already have (Phase 4's bot, Phase 5's HTTP surface) with no new long-lived connection type either service has to hold open and reconnect on drop, at the cost of a few seconds of latency and some idle polling overhead while a project is active -- an acceptable tradeoff for a single-operator, on-demand-started stack (ADR-010) that isn't optimized for sub-second notification latency |
| 028 | Polyglot project support (Phase 17) ships as a fixed, operator-maintained set of per-language workspace images (Python, Node to start) selected by the project's `stack`, with dependencies installed at container-start time from the project's own manifest file (`requirements.txt`, `package.json`) into a per-project cache volume -- **not** by giving any agent-reachable code path the ability to build or customize a Docker image | Directly requested (an agent that can build/customize its own workspace images, "no matter the language"), and directly in tension with ADR-005: `docker-socket-proxy`'s `BUILD=0` and "mount decisions are made by Executor code, never by an LLM tool call" were deliberate choices, not oversights, because an LLM-authored Dockerfile is LLM-authored code with build-time root execution, and enabling `BUILD` moves the trust boundary materially closer to host-root-equivalent access. Two alternative designs were considered and rejected for now: (a) a narrow, Executor-templated `tool_build_project_image` taking only structured parameters (base image, package list) with the Executor -- not the LLM -- rendering and building the Dockerfile, which is safer than free-form Dockerfiles but still needs `BUILD=1` and its own threat-model pass before it's defensible; (b) full agent-authored Dockerfiles, rejected outright as re-opening exactly what ADR-005 closed. The chosen design gets "any language" (the actual goal) without touching `BUILD` at all -- language support becomes "does Foundry ship an image for this stack yet," a much smaller, purely additive question than "can the agent build arbitrary images." Revisit (a) only if a real project's needs (e.g. a system package no base image ships) can't be met by adding it to a base image instead |
| 029 | `node_done` (Phase 13) always attempts a local git commit autonomously -- `git_branch` defaults to `feature/{project_id}` when `ProjectState.git_branch` is unset, rather than the node being a no-op without it -- but does **not** default `git_remote_url`; a project with no remote configured gets an autonomous local commit and no push/PR, rather than nothing at all | A first Phase 13 implementation gated the entire node on both `git_branch` and `git_remote_url` being present on state, but nothing anywhere in the graph ever set either field, making the Integrator's git logic permanently dead code in every real run -- directly contradicting this phase's own goal ("feature-branch commits always happen autonomously"). `git_branch` is always mechanically derivable from `project_id` with no external dependency, so defaulting it closes that gap completely. `git_remote_url` is different: the project registry (§4.6, ADR-013) already carries a `git_remote` field for exactly this purpose, but threading registry data into `ProjectState` at intake time is the same pre-existing gap Phase 7's and Phase 8's own verification notes already flagged and deferred (registry fields like `coverage_threshold`/`pr_mode` aren't wired into `new_project_state()` either) -- closing it here would have silently expanded Phase 13's scope into that gap. Caught in a Phase 13 validation pass; local-commit autonomy ships now, remote push/PR autonomy waits for whichever phase closes the registry-to-`ProjectState` wiring gap |
| 030 | `services/orchestrator/agents/__init__.py` is a docstring-only module with no re-exports at all | Found during a Phase 16/17 validation pass: it was eagerly importing and re-exporting symbols from `coder.py`/`memory_curator.py`/`planner.py`, which made importing *any* submodule of `agents` -- even a LangChain-free one like `tester.py` -- transitively require `langchain_core`, the exact same bug class Phase 6 already fixed once for `services/orchestrator/__init__.py` and `langgraph`. It had silently regressed at the `agents` package level sometime during Phase 15's LangChain migration. Confirmed via `grep` that nothing in the codebase imports symbols from the package root before stripping it down; this is what unblocked `agents/tests/unit/test_tester.py` running for real in a sandbox without `langchain_core` installed |
| 031 | Phase 17's cache-volume cleanup ships as two standalone, tested primitives (`containers.clear_project_cache_volume`, `tools.tool_clear_project_cache`) with nothing calling them yet, rather than also building a project-archival/removal flow to wire them into | TODO.md's Phase 17 task list assumes an "archived/removed from the registry" event already exists to hook cache cleanup into, but no such flow exists anywhere in this codebase -- `registry/registration.py` only ever adds projects; there is no `archive_project`/`remove_project` function of any kind in any phase completed so far. Building one was judged out of Phase 17's actual scope (a new registry capability, not a polyglot-workspace concern) and against CLAUDE.md's phase-discipline rule against implementing later-phase features to unblock a current one. The primitives exist and are tested now so that whichever future phase adds project archival only needs to call them, not design them |
| 032 | The cost-threshold gate (ADR-009) sums every *other* active project's latest checkpointed `cost_usd_weekly` (a new `cost_aggregator.py` helper reading only each thread's newest checkpoint, not every historical version) plus the current project's own live total, replacing the Phase 5 stub's per-project `cost_usd_session` check entirely; local (Ollama-backed) model calls are serialized through one `threading.Semaphore(1)` per `ModelRouter` instance, wrapped transparently behind `resolve()`'s existing return value so no agent module needed to change (Phase 18) | The stub gate never actually matched ADR-009 ("globally configurable" cost threshold) -- it compared one project's own session cost to a hardcoded constant. `checkpointer.list(None)` returns every historical checkpoint version per thread, not just the latest, so a naive full-history sum would double/triple-count a single project's spend; the aggregator reads only each thread's latest via `get_tuple()`. `threading.Semaphore` (not `asyncio.Semaphore`) is required because every LLM-backed node bridges to its async call via its own `asyncio.run()` (ADR-019) -- concurrent projects each get their own OS thread and event loop, which an `asyncio.Semaphore` scoped to one loop can't coordinate across |
| 033 | Skill evals (`skills/_eval_lib/`) are a standalone harness independent of `services.orchestrator.model_router`/`config/models.yaml`, calling Anthropic/Ollama directly over `httpx` with an injectable `CompleteFn`, rather than resolving roles through `ModelRouter` (Phase 19) | Only 4 of the 7 packaged skills (`develop-python`, `review-code`, `frontend-review`, `roast-the-plan`) map to a `ModelRouter` role that exists today; `test-and-coverage` and `git-ops` are backed by fully deterministic code with no LLM role at all, and `docs-writer` has no agent module yet. Coupling skill evals to Foundry's own routing config would have meant adding 3 speculative roles to `models.yaml` for skills with no graph node (phase-discipline violation), and would contradict ADR-014's actual point -- skills must work identically in Foundry, Claude Code, or any other harness, so their evals must not depend on Foundry's internal wiring either. `CompleteFn`'s injectable signature (`(system_prompt, user_prompt, image_bytes) -> str`) is what makes the harness's parsing/matching logic (`cases_schema.py`, `harness.py`, `cli.py`) genuinely unit-testable with a fake completion function, while the one module that actually reaches a network (`models.py`) stays thin and is reviewed by hand where no `ANTHROPIC_API_KEY`/Ollama instance is available to exercise it for real |
| 034 | `task_queue` items carry an explicit `status` ("pending"/"in_progress"/"done"/"blocked") and `attempts` count, and are never removed from the list on completion; the separate `current_task` field is deleted from `ProjectState` (Phase 22 stretch) | `task_queue` previously tracked completion only by list removal (`task_queue[1:]`), with the active item duplicated into a free-form `current_task` dict carrying an ad hoc `attempts` key. This meant a permanently-blocked task (attempts exhausted) was left sitting in both `current_task` *and* unchanged in `task_queue[0]` forever — two copies of one logical entity with no shared status. A single in-place status list is both the fix for that duplication bug and what actually satisfies Anthropic's published guidance on long-running coding-agent harnesses: a persistent, machine-readable task list with per-item status, readable by a resumed session or a `/status` query (TODO.md Phase 22) |
| 035 | `OrchestratorService`-level graph tests (`test_service.py`, `test_api.py`) get their own `fast_track_router_with_curation()` fixture, scripting a `CurationResult` response at each Memory Curator checkpoint, instead of reusing plain `fast_track_router()` | Found during Phase 22 validation: `node_memory_curator` fires a real LLM call whenever `phase_events` (derived from `messages`) is non-empty, which is *always* true for any project started via `OrchestratorService.handle_webhook()` (it seeds `initial_message` unconditionally) — unlike the graph-level fast-track tests (`test_graph_end_to_end.py`, `test_integrator.py`, `test_concurrency.py`), which deliberately seed no `initial_message` and rely on `messages` staying empty to keep the curator a no-op. Because `FakeRouter`'s `_FakeStructuredRunnable` pops the next queued response regardless of which schema was requested, this silently desyncs `fast_track_router()`'s shared response queue for any `OrchestratorService`-level test that resumes past a curator checkpoint: the curator consumes a response meant for `node_task_decomposition`, fails with a swallowed `AttributeError`, and the next real call then raises an uncaught `TypeError`. This affected 3 pre-existing tests (`test_resume_known_project_progresses_through_merge_gate_to_done` in both files, `test_status_of_a_terminal_project_clears_its_progress_buffer`) from Phase 14/18 that had never actually been run for real in any session, since this sandbox has never had `langgraph` installed (a gap since Phase 5) — only hand-traced, which never caught the desync. A single project's full resume cascade hits exactly three curator checkpoints (after `plan_wait`, after `testing`, after `merge_wait`), so the new fixture scripts exactly those three slots per round. See ADR-036 for the two list-projects tests, which this ADR's original fix left broken by a sibling bug in the same shared-queue pattern |
| 036 | Introduced `start_only_router(projects: int)` in `planner_fakes.py`, scripting only the 3-item Planner interview→draft→critique sequence per project; `test_list_projects_returns_known_ids`/`test_list_projects_returns_created_ids` switched from `fast_track_router(rounds=2)` to this instead | An independent review of ADR-035's commit caught that it left these two tests still broken by a *sibling* desync, not the one it fixed: `fast_track_router(rounds=N)` groups each round's 6 responses (3 Planner + `ONE_TASK`/`CODER_DONE`/`REVIEWER_APPROVE`) for one project's full start-then-resume cascade, but these two tests each drive a *different*, independent project through exactly one `handle_webhook` call and never resume. Project A's call consumes only the first 3 (Planner-shaped) items of round 1 before pausing at `plan_wait`; project B's call then starts popping at index 3 — `ONE_TASK`, a `_TaskList` — where `node_planner_ask` expects an `InterviewTurn`, raising `AttributeError` on `turn.status`. `test_concurrency.py`'s existing `_start_script()` helper already isolated the correct 3-item shape for exactly this situation; `start_only_router()` generalizes it into a reusable fixture rather than duplicating it a third time |
| 037 | `ProjectState` gains `messages_summary: str`, `messages_curated_through: int`, and `changed_files: list[str]` (plain fields, no reducer -- same "read current value, write the new total" idiom already used for `cost_usd_session`/`cost_usd_weekly`); `node_memory_curator` folds each successful curation into a rolling summary and advances a cursor instead of reading a hardcoded `messages[-20:]` window; `CoderTaskResult` gains `written_files: list[str]` (captured directly off dispatched `write_file` tool calls), which `node_coder` accumulates into `changed_files` across every task/retry and `node_reviewer` passes into `review_changes()`'s existing, previously-unused `changed_files` parameter; `_task_progress_lines()` caps rendered `task_queue` entries to non-`done` tasks plus the 10 most recent `done` ones (Phase 22 stretch) | TODO.md flagged three unbounded prompt-construction vectors: `messages` (only bounded by a hardcoded, silently-lossy `[-20:]` slice in the curator, and read in full by the Planner's interview with no cap at all), `task_queue` (rendered into the curator's prompt in full, forever), and the Reviewer's file-tree read (`review_changes()` already accepted a `changed_files` parameter but `node_reviewer` never passed it, so every review re-read the entire repo). A plain additive summary field was chosen over switching the `messages` reducer to LangGraph's `add_messages`/`RemoveMessage` (which would allow real deletion, not just a bounded read) because that path depends on `BaseMessage` auto-id behavior unverifiable against the pinned `langgraph>=0.2` floor in this sandbox (no real install to check against, and this session had two fixes back-to-back turn out to have hand-trace-only sibling bugs on this exact file) -- the additive approach matches an idiom this codebase already trusts instead of introducing a new one. Design self-review caught that `_extract_description_and_history`'s consumption of the cursor is currently inert in practice: `node_planner_ask` only ever runs before `plan_wait`, and `node_memory_curator` (the only thing that advances the cursor) only ever fires after it -- there is no path back to the Planner post-approval yet (no re-planning phase, a separate, unbuilt Phase 22 stretch item), so the cursor is always `0` at every point that function runs today. Included anyway as forward-proofing, since it shares the same fields the curator fix needs regardless. Two limitations accepted rather than solved: `messages_summary` isn't itself re-summarized if it grows large over an extremely long-lived project, and files modified via `run_command` shell redirects instead of `write_file` escape `changed_files` tracking (the Coder's system prompt steers toward `write_file` but doesn't enforce it) |
| 038 | `node_memory_curator`'s task-progress inclusion now checks `state["messages"]` (has this project ever had any messages at all) rather than the cursor-sliced tail being non-empty, before deciding whether to append `_task_progress_lines(...)` to `phase_events` | ADR-037's final whole-branch review caught that gating task-progress inclusion on the message *tail since cursor* being non-empty meant the curator silently stopped firing at the `testing`/`merge_wait` checkpoints on any smooth happy-path project (nothing appends new messages between phase transitions unless a reviewer/testing feedback loop happens), defeating the point of feeding task outcomes into curation for the common case. Checking `state["messages"]` overall instead preserves the separate, deliberate invariant `test_concurrency.py` relies on (a project that never seeds `messages` keeps the curator permanently silent even once `task_queue` is populated), while restoring curation at every checkpoint for any project that has ever had a seeded `initial_message` -- which also makes `fast_track_router_with_curation`'s three scripted `CurationResult` responses per round live again, instead of two of them silently going unused |
| 039 | Gateway `/status` and `/projects` are read-only lookups against `GET /status/{id}` / `GET /projects`; only `/new` and free text reach `POST /webhook`. An approval button press relays the resume response back into the chat (next gate's keyboard, task progress, or a terminal-phase message) instead of discarding it | A whole-project audit found both commands were forwarded as free text to the webhook, which starts a *new project* for any unknown/absent project_id -- every `/status` check spawned a junk project plus a real Planner LLM call. Separately, `handle_approval_callback` ignored the resume response entirely, so a project advancing straight to its next gate (plan approval -> merge gate) never got that gate's keyboard pushed; the flow stalled until the user happened to send another message (the long-standing "notify via Telegram not wired" gap flagged since Phase 9, now closed for the resume path) |
| 040 | `OrchestratorService.handle_webhook` applies `registry.validation.validate_project_id`'s slug allow-list to any caller-supplied project_id before starting a new project; the API maps the failure to 400 | The webhook entrypoint bypassed the registry's validation entirely: an arbitrary project_id became a LangGraph thread_id and, once the Memory Curator fired, a filesystem path component (`projects/<id>/memory/`), so `project_id="../../etc"` wrote outside the projects root. Only reachable from the internal network today (the Gateway never sends project_id), but input that becomes a path component gets the same structural allow-list everywhere, not just in the registry |
| 041 | The Coder recalls cross-project lessons via a new `recall_for_task(conn, task_description)` (`memory/global_store.py`) -- alnum tokens, lowercased so none collide with FTS5's case-sensitive uppercase operators, OR-joined, failures logged and swallowed -- and `run_coder_task` appends the results to its task prompt; `node_coder` takes the global memory connection (None keeps recall off for queue-mechanics tests) | `recall()` had zero production callers: Phase 14 wired the *write* side (curator) into the graph but no agent ever read the store back, so the cross-project memory layer was write-only in the live system and Phase 14's own "project B's Coder has access to project A's fix" claim held only inside tests that called recall() directly. The Coder's per-task prompt is the natural read point; the sanitizing wrapper exists because `recall()` takes a raw FTS5 MATCH expression, where quotes/parens/bare-operators in free task text raise sqlite3.OperationalError |
| 042 | `IntegratorClient.commit_and_push` checks out the feature branch *before* committing and pushes without `--force`; pushing requires the target project's own git config to carry credentials (deploy key / credential helper) -- Foundry-level tokens are never injected into workspace containers | The audit found an unconditional force push (git.md forbids force pushes outright) and a commit-then-branch ordering that landed every commit on main first ("all work happens on a branch" applies to generated projects too). The credential stance is deliberate: `run_command` inside a workspace container is an agent-reachable surface, and .claude/rules/security.md bars the GitHub token from ever reaching one -- so a push to a credential-less remote fails cleanly, leaving the local commit, rather than Foundry quietly threading its token into agent reach |
| 043 | `ExecutorClient` opens a short-lived `httpx.AsyncClient` per request (with a 600s read timeout) in real deployments; tests still inject a MockTransport-backed client | Every graph node bridges to async via its own `asyncio.run()` (ADR-019), so the single AsyncClient previously built once in main.py pooled keep-alive connections on one node's (closed) event loop and reused them on the next -- httpx's classic "Event loop is closed" failure, worse with concurrent projects on separate threads (Phase 18). Per-request clients trade connection reuse for cross-loop correctness. The explicit timeout matters independently: httpx's 5s default would abort every long run_command/run_tests call client-side while the Executor's own command timeout is 300s |
| 044 | The three core services build from real per-service Dockerfiles (repo-root context, per-service dependency installs mirroring pyproject floors, `PYTHONPATH`-run tree -- the flat multi-package repo is deliberately not pip-installable); `config/` is volume-mounted read-only into the orchestrator with `MODELS_CONFIG_PATH` env-overridable; `.dockerignore` keeps `secrets/` and `projects/` out of every build context | The compose file had shipped `python:3.12-slim` + `sleep infinity` placeholders since Phase 1, so no phase's Definition of Done that implied a *running* service was ever achievable end-to-end via `./scripts/start.sh`. Mounting config rather than baking it preserves CLAUDE.md's "swap models in config, not code" rule across image rebuilds; the context exclusion exists because a repo-root build context would otherwise upload real deployment secrets to the Docker daemon on every build even though no COPY references them |
| 045 | Language support becomes config-driven: a single `config/stacks.yaml` (Pydantic-validated, like `models.yaml`) declares each stack's upstream base image, apt packages, manifest filenames, dependency-install command, package-cache directory, and static-check commands; per-stack Dockerfiles are rendered by an operator-run generator from one shared hardening template (non-root `sandbox` UID 1000, setuid strip, Python3 + Playwright tool layer), replacing hand-copied Dockerfiles; `image_for_stack()`, install-command selection, cache paths, and registry `stack` validation all read the same YAML instead of hardcoded dicts/Literals | Scoped to a known operator-chosen shortlist (Go next) after explicitly revisiting ADR-028: the agent-built-image door stays shut (`BUILD=0`, no agent-reachable Dockerfile path), so "add a language" stays a purely additive operator action — one YAML entry plus one image build, zero `.py` edits. The template exists because `workspace-node/Dockerfile` already had to mirror `workspace-python`'s security posture by hand-copy, which is exactly how hardening drift starts; mirroring the `models.yaml` "swap in config, not code" rule keeps six previously scattered edit points (STACK_IMAGES, Stack Literal, CLI choices, install commands, cache paths, check commands) in one file |
| 046 | A deterministic `static_checks` graph node runs between `coder` and `reviewer`: it executes the stack's check commands from `stacks.yaml` (e.g. `ruff check` for python, `tsc --noEmit` for node, `go vet`/`go build` for go) inside the workspace container via the existing `tool_run_command`; failure routes back to `coder` with the raw tool output as feedback and increments the active task's `attempts` (honoring `MAX_TASK_ATTEMPTS`); a check whose tool or config is absent from the project is skipped, not failed | Loop-engineering principle (LangChain "The Art of Loop Engineering", Osmani "Loop Engineering", 2026): cheap deterministic verification before expensive LLM verification. Today every Coder iteration reaches the LLM Reviewer even when it doesn't lint or compile — spending Reviewer tokens to restate what a linter says for free. Folding the checks into `node_reviewer` was rejected (breaks one-purpose-per-node, makes the no-LLM path untestable in isolation); counting failures against the existing attempt budget keeps the loop's termination guarantee unchanged; skip-if-absent keeps the node honest on projects that legitimately lack a given tool |
| 047 | A human-gated replan route (promoting the Phase 22 stretch item): the Reviewer verdict schema gains `replan_needed` (with reason), and a task exhausting its attempts routes to a new `replan_wait` `interrupt()` instead of a terminal halt; the human decides via Telegram — approve re-invokes the Planner with the failure context and completed tasks preserved (the normal plan-approval interrupt then fires again on the new plan), reject halts as today; a `replan_count` state field caps replans at 2 per project | Once a plan passes `awaiting_plan_approval` it is frozen — Reviewer/Tester can only say "fix this diff," never "this task rests on a false assumption," so a doomed plan burns all its attempt budget and strands the project. Automatic (ungated) replanning was rejected: plan changes are precisely what Foundry's supervision model gates on — silently swapping the plan out from under the human contradicts the existing plan-approval interrupt. The cap preserves provable termination; the standard interrupt test contract (fires → survives checkpointer restart → resumes) applies |
| 048 | Third provider type `openai_compatible`: `ChatOpenAI` (langchain-openai) pointed at a configured `base_url`, throttle-wrapped with a per-endpoint semaphore (one per `base_url`); constructed whenever declared in the loaded config — declaration is the enable switch, no env gate; fixed placeholder API key (authenticated endpoints out of scope); ships with the `config/models.remote-llm.yaml` preset | The deployment's GPU may live on another machine (e.g. an RTX 3090 PC running LM Studio) where `--local-llm`'s in-host Ollama container is useless; the OpenAI chat-completions API covers every common self-hosted server (LM Studio, vLLM, llama.cpp), so one generic type beats per-vendor types (YAGNI). ADR-015's construction-time gate maps to a Compose profile that doesn't exist for a remote server — the config file is the only honest availability signal, and an unreachable server fails after ADR-025 retries exactly like a Claude outage. Per-endpoint (base_url-keyed) semaphore rather than the shared Phase 18 one because that semaphore models one in-host GPU, while each remote endpoint is its own GPU: two endpoints must not serialize against each other, but calls to one endpoint never overlap |
| 049 | `agents/llm.py`'s `invoke_structured()` forces `method="function_calling"` on every `with_structured_output()` call, instead of leaving the method provider-inferred | Found via a real `openai_compatible` deployment (a reasoning-mode local model behind LM Studio): left unspecified, `ChatOpenAI` silently resolves to OpenAI's strict `json_schema` structured-output mode, which the model failed outright on — it exhausted its turn on its `<think>` block and returned empty content with neither a `parsed` nor a `refusal` field, never entering the JSON grammar. Tool-calling is the one structured-output mechanism every provider this router supports (`ollama`, `anthropic`, `openai_compatible`) handles reliably, including reasoning-mode models, which are commonly tool-call-trained even when their strict-JSON-grammar compliance is unreliable |
| 050 | `AnthropicProviderConfig` gains an optional `base_url` (default `None` = the real hosted Claude API); when set, the router throttles it exactly like an `openai_compatible` provider, sharing the same `base_url`-keyed semaphore dict (ADR-048) | ADR-049's `function_calling` fix stopped the local model from failing outright, but LM Studio's OpenAI-compatible endpoint only accepts the plain-string `tool_choice` values (`none`/`auto`/`required`), not OpenAI's named-tool object form `with_structured_output(method="function_calling")` sends — a second, purely mechanical incompatibility, confirmed via a direct curl 400. Verified via a raw HTTP request (bypassing Foundry) that LM Studio's separate Anthropic-Messages-API-compatible endpoint accepts the equivalent named `tool_choice` and, unlike its OpenAI-compatible endpoint, correctly surfaces a reasoning model's post-thinking answer in a `tool_use` content block instead of folding everything into a non-standard `reasoning_content` field with nothing left in `content` — Anthropic's protocol has first-class support for a model reasoning before answering in the same turn; OpenAI's Chat Completions API does not. Reuses `ChatAnthropic`, the already-proven code path, rather than adding a fourth provider type; `openai_compatible` remains the right choice for self-hosted servers with no Anthropic-compatible surface |

---

## 11. Out of Scope for v1 / Future Work

- **Neo4j memory upgrade.** If cross-project pattern recall ("this error/fix pattern showed up in 3 projects") becomes valuable enough to justify the operational cost, add it as an additional structured layer underneath the markdown files — not a replacement for them. Neo4j's native vector index could also absorb semantic search duties without a 4th datastore.
- **Observability stack (Prometheus/Grafana).** Deliberately deferred. When added, instrument the Orchestrator and Executor with a `/metrics` endpoint and ship a dashboard — natural fit given the operator's professional background, but not a v1 blocker.
- **A2A protocol exposure.** If Foundry's agents should ever be callable from another A2A-compliant system, or call out to one, expose an Agent Card and adopt the task lifecycle — not needed while Foundry is a closed, single-operator system.
- **MCP servers.** Filesystem and GitHub MCP servers are candidates for Phase 2+ once the custom Executor/Git-Integrator tools prove insufficient for some workflow; not required for v1 since the narrow typed tools (§4.3) already cover the core loop.
- **AMD 6750XT / ROCm parity.** Ollama's ROCm support is less mature than CUDA. Treat the 6750XT as a secondary inference target for smaller helper roles (e.g. memory curation) rather than assuming feature parity with the 3090 from day one.
- **Docker Sandboxes / microVM isolation.** If Docker's Sandboxes product matures into something Compose-native, it's a strictly stronger replacement for the socket-proxy pattern in §4.3 — worth revisiting, not worth blocking v1 on.

---

## 12. Glossary

- **Workspace** — an ephemeral, isolated container where a single task actually executes (read/write only within the mounted project directory).
- **Executor** — the trusted service that is the sole owner of the Docker control surface and exposes narrow typed tools to agents.
- **Capability** — a declared requirement of an agent role (e.g. `vision`, `tool_calling`, `long_context`) used by the Model Router to pick a provider.
- **Phase** — a stage in a project's lifecycle (`intake`, `implementing`, `testing`, etc.) tracked in `ProjectState.phase`.
- **HITL gate** — a point where the graph calls `interrupt()` and waits for a Telegram-driven `Command(resume=...)`.
