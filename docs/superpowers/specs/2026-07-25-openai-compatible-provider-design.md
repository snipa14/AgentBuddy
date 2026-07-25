# Design: `openai_compatible` model provider (remote LM Studio support)

Date: 2026-07-25
Status: approved

## Problem

Foundry's model router supports exactly two provider types: `ollama` (an
Ollama container on the Foundry host, started by the `--local-llm` Compose
overlay) and `anthropic` (Claude API). A deployment whose GPU lives on a
*different* machine — e.g. an RTX 3090 PC running LM Studio's
OpenAI-compatible server — has no way to use it: passing `--local-llm`
downloads and runs Ollama on the (GPU-less) Foundry host instead.

## Decision

Add a third provider type, `openai_compatible`, that points the router at
any server speaking the OpenAI chat-completions API (LM Studio, vLLM,
llama.cpp server, ...). Chosen over (B) requiring Ollama on the remote PC —
does not solve the actual request — and (C) an LM-Studio-specific SDK
integration — extra dependency, does not generalize (YAGNI).

## Config schema (`model_router/config.py`)

New Pydantic model, added to the existing `ProviderConfig` discriminated
union:

```python
class OpenAICompatibleProviderConfig(BaseModel):
    type: Literal["openai_compatible"]
    model: str
    base_url: str
    capabilities: list[Capability] = Field(default_factory=list)
    cost_per_1m_input_tokens: float = Field(ge=0)
    cost_per_1m_output_tokens: float = Field(ge=0)
```

Validation, unknown-provider checks in role `prefer` lists, and
`cost_usd()` accounting work unchanged through the union.

## Preset (`config/models.remote-llm.yaml`)

Follows the `models.claude-only.yaml` pattern: identical `roles:` to the
default config, with the `local` provider replaced by:

```yaml
local:
  type: openai_compatible
  base_url: http://<gpu-pc-lan-ip>:1234/v1
  model: <model id as served by LM Studio>
  capabilities: [tool_calling]
  cost_per_1m_input_tokens: 0
  cost_per_1m_output_tokens: 0
```

Keeping the provider name `local` means no role `prefer` chain changes.
Activated by setting `MODELS_CONFIG_PATH=/app/config/models.remote-llm.yaml`
in `.env`/compose — the same mechanism as the claude-only preset. The user
fills in the placeholder `base_url`/`model` values for their deployment.

## Router (`model_router/router.py`)

Third branch in `ModelRouter.__init__`:

- Builds `ChatOpenAI` (new orchestrator dependency: `langchain-openai`)
  with the config's `base_url` and `model`, a fixed placeholder API key,
  and `max_retries=PROVIDER_MAX_RETRIES` (ADR-025 parity).
- Wraps it in the existing `_ThrottledChatModel` with a **per-provider**
  `threading.Semaphore(1)` — not the shared Ollama semaphore. Phase 18's
  shared semaphore models "one in-host GPU"; a remote server is a different
  physical GPU. Each remote provider still serializes to one in-flight
  request, but two distinct GPUs never falsely serialize against each
  other.
- **Gating:** constructed whenever declared in the loaded config. There is
  no container or env flag to gate on (deliberate deviation from ADR-015's
  construction-time gating — declaring the provider in the active config
  *is* the enable switch).

`resolve()`, `configured_providers()`, `cost_usd()`, and capability
matching are unchanged.

**API keys:** LM Studio ignores the key, but the OpenAI client requires
one, so the router passes the fixed module-level constant
`OPENAI_COMPATIBLE_PLACEHOLDER_API_KEY = "not-needed"` (not a secret).
Endpoints requiring real authentication are out of scope; the schema
leaves room for an optional secrets-backed key field later.

## Deployment

- `scripts/start.sh` unchanged; run it *without* `--local-llm`. No Ollama
  container, no downloads.
- One `.env` addition: `MODELS_CONFIG_PATH=/app/config/models.remote-llm.yaml`.
- The orchestrator reaches the GPU PC's LAN IP as ordinary outbound
  traffic from the default bridge network; no compose changes.

## Error handling

- Config errors (bad `type`, missing `base_url`) fail at orchestrator
  startup via Pydantic `ValidationError`.
- An unreachable LM Studio server fails at call time after 5 retries and
  surfaces through the existing agent error paths — the same behavior as
  an Anthropic outage. No silent mid-run fallback to Claude: prefer chains
  are resolved by configuration and capability, not liveness (unchanged
  from today's Ollama semantics).

## Testing (`model_router/tests/unit/`)

- `test_config.py`: a valid `openai_compatible` block parses; missing
  `base_url` or unknown `type` raises `ValidationError`.
- `test_router.py`:
  - constructs a `ChatOpenAI` with the configured `base_url`/`model`;
  - the resolved model is `_ThrottledChatModel`-wrapped;
  - two `openai_compatible` providers get independent semaphores while
    `ollama` providers still share one;
  - retries are enabled on the constructed model;
  - `resolve("coder")` returns the remote provider when it declares
    `tool_calling`.
- No live-network integration test (testing rules: don't test third-party
  library behavior).

## Docs

- Update `docs/model-router.md` with the new provider type.
- Append `ARCHITECTURE.md` Decision Log entry (ADR-048): the
  `openai_compatible` type, declaration-in-config as the enable gate, and
  per-provider throttle semaphores for remote single-GPU servers.
