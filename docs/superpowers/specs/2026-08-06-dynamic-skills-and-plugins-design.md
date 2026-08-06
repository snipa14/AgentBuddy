# Dynamic Skills & Plugins: Design

**Status:** Draft (pending user review)
**Decisions depended on:** ADR-014 (skills as portable `SKILL.md`), ADR-025 (structured output via `with_structured_output`), ADR-046 (fixed extension points in the graph, e.g. `static_checks`), §11 (MCP explicitly out of scope for v1)

## Goal

Make Foundry's LangGraph runtime actually *use* the skills-as-files promise ADR-014 already made, and let an operator extend the agent pipeline with new roles by dropping files into a directory before startup — auto-detected and loaded, no code change — without weakening the security boundaries in `.claude/rules/security.md`.

This spec covers two of three layers identified during brainstorming; the third is explicitly deferred (see Out of Scope).

## Current State

- `skills/<name>/SKILL.md` already exists for all 7 documented roles (Phase 19), each with an `eval/` harness (`skills/_eval_lib/`) that reads the file for real.
- The *production* graph nodes don't read these files. `services/orchestrator/agents/planner.py` (and equivalently `coder.py`, `reviewer.py`) hard-code their system prompts in Python, with only a docstring/comment pointing at the matching `SKILL.md`. The eval harness and the live agent have quietly diverged.
- There is no plugin concept anywhere in Foundry's own runtime. The `superpowers` Claude Code plugin is used only as this repo's own development methodology (brainstorm → spec → plan → implement), never wired into `services/orchestrator/graph`.
- The graph (`graph/build.py`) is a fixed sequence of hand-wired nodes and edges, compiled once at process startup. `static_checks` (ADR-046) and `frontend_review` (Phase 12) are the two precedents for "a deterministic or conditional step slotted into an otherwise linear pipeline."
- MCP servers are explicitly deferred in §11 ("candidates for Phase 2+ once the custom Executor/Git-Integrator tools prove insufficient") and Claude Code plugins commonly bundle exactly that, plus hooks (arbitrary shell scripts on tool events) and slash commands — neither of which map onto Foundry's typed-tool, Telegram-driven runtime.

## Decision: Scope for This Spec

Three layers of increasing risk were identified; only the first two are designed here:

1. **Dynamic skill loading** — no new security surface, just making existing agent nodes read the `SKILL.md` files that already exist instead of ignoring them.
2. **Dynamically-registered new agent roles**, using Foundry's *existing* typed Executor tools and the existing verdict/comments contract (Reviewer/static_checks) — changes graph *construction*, not the security boundary.
3. **Plugin-supplied MCP servers / new tools** — deferred. This is the one layer that would let third-party code become an LLM-callable tool, which directly intersects the Executor-only-Docker rule (`.claude/rules/security.md`) and §11's existing MCP stance. Revisit only once layers 1–2 are proven and a concrete need the existing typed tools can't cover actually shows up — same bar §11 already sets for MCP generally.

Plugin-supplied **hooks** (arbitrary shell scripts) and **slash commands** are out of scope permanently for this design, not just deferred — Foundry has no CLI/REPL surface a slash command would attach to, and a third-party-authored hook running with real system access is the same trust problem as layer 3, in a different shape.

## Architecture

```
plugins/
  <plugin-name>/
    plugin.yaml         # manifest — see schema below
    SKILL.md            # portable skill body, same Agent-Skills format as skills/*/SKILL.md

skills/
  loader.py             # NEW — load_skill(name) -> SkillContent, cached; used by every
                         # agent module (built-in and plugin) instead of inline prompt strings
  develop-python/ ...    # existing skills, unchanged in format

services/orchestrator/
  plugins/
    discovery.py         # NEW — scan plugins/ once at startup, parse + validate manifests
    manifest.py           # NEW — PluginManifest Pydantic schema
  graph/
    build.py               # extended — after building the fixed graph, wire in each
                            # surviving plugin at its declared slot
    plugin_node.py           # NEW — one generic node-runner reused by every plugin role
  model_router/
    config.py                # extended — synthesize a role entry per surviving plugin
                              # from its `requires:` list, unless models.yaml already
                              # defines that role name (explicit config always wins)
```

Built-in roles (`coder`, `reviewer`, `tester`, `planner`, `frontend_reviewer`) are **not** migrated into `plugins/`. They stay hand-wired graph nodes exactly as today; only their prompt construction changes, from an inline string to `skills.loader.load_skill("develop-python")` embedded inside the same Foundry-owned wrapper (response-format instructions, parsing contract) that already exists. This keeps the actual skill content portable — usable unmodified in Claude Code, per ADR-014's original point — while Foundry-specific plumbing (e.g. "respond in this exact `## Verdict` markdown for my parser") stays in Python where it belongs, not baked into the skill file.

### Plugin manifest (`plugin.yaml`)

Foundry-specific format, not a mirror of Claude Code's `.claude-plugin/plugin.json` — that schema carries hooks/`mcpServers`/`commands` fields Foundry can't act on under this spec's scope, and tracking an upstream schema for fields that do nothing is worse than a minimal purpose-built one. Portability is preserved at the right layer instead: the `SKILL.md` body stays the real Anthropic Agent Skills format, so skill *content* is still reusable elsewhere even though the manifest wrapper isn't.

```yaml
name: security-audit-reviewer
role: security_audit_reviewer      # becomes the model_router role name
slot: after_reviewer               # after_coder | after_reviewer | after_tester
mode: gating                       # gating | advisory
requires: [tool_calling]           # same shape as an existing role's requires: in models.yaml
skill: SKILL.md                    # path relative to the plugin directory
```

- `slot` — one of three fixed extension points, mirroring where `static_checks` (`after_coder`) and `frontend_review` (`after_reviewer`) already sit in the existing linear sequence. A plugin cannot declare an arbitrary position; multiple plugins may share a slot and run in declared order (ties broken by plugin directory name, ascending).
- `mode: gating` — the plugin's verdict can return `changes-requested`, routing back to Coder through the existing `attempts`/`MAX_TASK_ATTEMPTS` budget (ADR-034), exactly like `node_reviewer` does today.
- `mode: advisory` — same call, but the verdict is only ever surfaced into progress/memory; the graph never acts on `changes-requested` from an advisory plugin.
- `requires` — capability list for `model_router`. The loader synthesizes a `RouterConfig` role entry at startup using `requires` from the manifest and `prefer: [local, claude_sonnet]` — the same default chain `reviewer`'s own `models.yaml` entry uses today, since a plugin role's job (render a verdict on a diff) is the same shape as Reviewer's. If `config/models.yaml` already declares a role of that exact name, the file wins and the synthesized entry is skipped entirely — an operator who wants a specific cost/capability tier for a plugin role sets it the same way they would for any built-in role.

### Generic node-runner (`plugin_node.py`)

Every plugin role executes through one shared node function, parameterized by its manifest (skill body, capability role name, slot contract). It calls the model with the same structured `{verdict, comments}` output contract `review_changes()` already uses (ADR-025), so no new graph-routing logic is needed — a plugin role is structurally "review the current diff/changed_files and render a verdict," nothing more open-ended than that.

Plugin-supplied Python node code (a plugin shipping its own node function to be imported and wired in directly) was considered and rejected: that would be arbitrary third-party code running inside the Orchestrator process itself, which has DB/Executor-network/env reach — the same trust problem as a plugin-supplied hook, just relocated from a shell script to a Python import, and squarely what `.claude/rules/security.md`'s "if a feature seems to need broader access, stop and redesign" is warning against.

## Data Flow

**Startup:**
```
Orchestrator process start
  → skills.loader reads all skills/*/SKILL.md once (cached for process lifetime)
  → plugins.discovery scans plugins/*/plugin.yaml
      → parse + Pydantic-validate each manifest
      → check each manifest's `requires` capabilities resolve against the
        loaded model_router config (the same check ModelRouter already
        performs per-role — just run eagerly now, at startup, instead of
        deferred to first invocation)
      → invalid manifest, unsatisfiable capabilities, or a `role`/`name`
        collision with a built-in role or an already-accepted plugin
        → SKIP that plugin, log a startup warning naming it and the reason.
          Never crash the Orchestrator process; never silently include a
          broken plugin in the compiled graph.
  → graph/build.py builds the fixed graph exactly as today, then for each
    surviving plugin: add_node(plugin_node_runner bound to its manifest),
    wired into its declared slot's existing conditional-edge pattern
  → model_router config gains one synthesized role entry per surviving
    plugin lacking an explicit models.yaml entry
```

**Runtime, at a `gating` slot** (e.g. `after_reviewer`): identical in shape to how `frontend_review` already sits after `reviewer` — the plugin node runs, produces `{verdict, comments}`, `changes-requested` routes back to Coder through the existing attempts budget, `approve` falls through to the next node in the fixed sequence. An `advisory` plugin makes the same call but its `changes-requested` is never acted on by the graph, only logged.

## Error Handling

- Plugin discovery/validation failures: skip-and-log-loudly, never crash startup — same pattern `static_checks` (ADR-046) already uses for "check tool/config absent from this project."
- A plugin role that resolves fine at startup but whose provider becomes unreachable mid-run fails exactly like any existing role does today (ADR-025's retry/backoff, then a normal node-level failure) — no new error path needed.
- Duplicate `role`/`name` across plugins, or a plugin `role` colliding with a built-in role name: second one skipped, loudly, at discovery time.

## Testing

- `services/orchestrator/plugins/discovery.py` and `manifest.py`: dependency-free Pydantic/YAML validation, real runnable pytest in this sandbox (same tier as `registry/`, `memory/`) — including fixture manifests for each rejection path (malformed YAML, missing skill file, bad capability, duplicate role name).
- `graph/plugin_node.py`: unit tests mocking `model_router.resolve()`, same pattern as `test_reviewer.py`.
- `graph/build.py`'s plugin-wiring extension: hand-traced only, same permanent sandbox gap (`langgraph` not installed) as the rest of the graph layer since Phase 5.
- A plugin's `SKILL.md` reuses the existing `skills/_eval_lib` harness from Phase 19 unmodified — a plugin skill is structurally identical to a built-in one, so no new eval infrastructure is needed.
- A negative test proving the rejected alternative stays rejected: no code path in `discovery.py`/`plugin_node.py` ever calls `importlib`/`exec`/`eval` against plugin-supplied Python — the generic node-runner is the only thing that executes, parameterized by data (manifest + skill text), never by plugin code.

## Out of Scope

- **Plugin-supplied MCP servers / new tools** (layer 3). Deferred pending a concrete need the existing typed Executor tools can't cover, consistent with §11's existing MCP stance, and pending a dedicated security design for how a third-party tool implementation could run without breaking "only the Executor touches Docker" / "the LLM never touches infrastructure directly" (§2).
- **Plugin-supplied hooks** (event-triggered scripts). Permanently out of scope for this design — third-party code with real system access, no different in kind from layer 3's trust problem.
- **Plugin-supplied slash commands.** No analogous surface exists in Foundry (Telegram Gateway's commands are fixed and auth-gated, not user-extensible).
- **Per-project plugin opt-in / allow-lists.** This spec makes every discovered plugin global to all projects (simplest model, matches how `skills/` already works). A `projects.yaml` opt-in list is a natural follow-up if an operator running multiple projects needs to scope a plugin to one of them, but isn't required for the base mechanism to work.
- **Hot-reloading plugins into a running process.** Discovery happens once, at Orchestrator startup, matching "auto-detect before Foundry starts up." Adding/removing a plugin requires a restart, same as any `config/models.yaml` change today.
