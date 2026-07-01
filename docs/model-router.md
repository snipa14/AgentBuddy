# Model Router

`services/orchestrator/model_router/` resolves an agent role (`planner`, `coder`,
`reviewer`, ...) to a `Provider` instance, following the `prefer` fallback chain
declared for that role in `config/models.yaml`. See ARCHITECTURE.md §4.4 for the
design rationale and ADR-001/ADR-015/ADR-016 in §10 for the decisions behind it.

## Adding a new role

Add an entry under `roles:` in `models.yaml`:

```yaml
roles:
  docs_writer:
    requires: [tool_calling]
    prefer: [local, claude_haiku]
```

- `requires` is the list of `Capability` values (`tool_calling`, `long_context`,
  `vision`) the role needs. `resolve()` skips any preferred provider that
  doesn't declare all of them.
- `prefer` is tried in order; the first provider that's both configured
  (present in `providers:`) and capable is returned.
- Every name in `prefer` must exist under `providers:` — the config loader
  rejects the file otherwise, before the router ever runs.

No code change is needed to add a role, change its capability requirements, or
reorder/replace which providers it prefers.

## Adding a new provider

To add another named instance of an existing provider `type` (for example, a
new Claude tier), add an entry under `providers:`:

```yaml
providers:
  claude_haiku_fast:
    type: anthropic
    model: claude-haiku-4-5
    capabilities: [tool_calling]
```

`capabilities:` is declared explicitly per provider (ADR-016) — the router
never infers capabilities from `type`, so two providers of the same type can
have different capability profiles without touching Python.

Adding a genuinely new provider *type* (a third backend beyond Ollama and
Anthropic) does need code: implement the `Provider` protocol
(`services/orchestrator/model_router/provider.py`) in a new module under
`providers/`, add a matching `TypeProviderConfig` to the discriminated union in
`config.py`, and register construction for it in `ModelRouter.__init__`.

## The Claude-only preset

`config/models.claude-only.yaml` is a documented preset where every role's
`prefer` list contains only `claude_*` providers and `providers:` declares no
`ollama`-type entry at all. Drop it in unchanged as `config/models.yaml` for a
deployment with no GPU:

```bash
cp config/models.claude-only.yaml config/models.yaml
```

Because it declares no `ollama`-type provider, the router never constructs an
`OllamaProvider` and never attempts an Ollama connection for any role — this
holds regardless of whether `OLLAMA_ENABLED` is set. See ADR-015 for why local
availability is a startup-time decision rather than a per-call probe.

## Testing notes

- Unit tests for the router and providers live in
  `services/orchestrator/model_router/tests/unit/` and mock the injected
  `httpx.AsyncClient` — no real network call is ever made.
- Tests for LLM-backed *agent* nodes (Phase 8+) should mock at the
  `model_router.resolve()` boundary instead, per
  `.claude/rules/testing.md` — that tests the agent's handling of a
  completion, not the router's own resolution logic.
