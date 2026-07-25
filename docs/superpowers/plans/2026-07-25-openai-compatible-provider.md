# `openai_compatible` Model Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third model-router provider type, `openai_compatible`, so a deployment can route `local` roles to a remote OpenAI-compatible server (LM Studio on a LAN GPU PC) instead of an in-host Ollama container.

**Architecture:** A new `OpenAICompatibleProviderConfig` joins the existing discriminated union in `model_router/config.py`; `ModelRouter.__init__` gains a branch that builds a `ChatOpenAI` (from `langchain-openai`) pointed at the configured `base_url` and wraps it in the existing `_ThrottledChatModel` with a per-provider semaphore. A `config/models.remote-llm.yaml` preset mirrors the default config with `local` re-pointed at the remote server. Spec: `docs/superpowers/specs/2026-07-25-openai-compatible-provider-design.md`.

**Tech Stack:** Python 3.12, Pydantic v2, langchain-openai (`ChatOpenAI`), pytest.

## Global Constraints

- Python 3.12; ruff `line-length = 100`; imports sorted (ruff `I`).
- **No inline comments** (CLAUDE.md) — docstrings only; single-line `#` only for non-obvious workarounds with an issue reference. YAML config files may carry explanatory `#` header comments (existing pattern in `config/*.yaml`).
- Google-style docstrings on every public function and class.
- All commits go **directly on `release/v1`** (no feature branch/worktree — established repo workflow). Conventional Commits format.
- Never hardcode a credential. The literal `"not-needed"` placeholder API key is not a credential (LM Studio ignores keys; the OpenAI client merely requires the field to be non-empty).
- **Sandbox gap (permanent, expected):** `langchain_core`, `langchain_openai`, `langchain_ollama`, `langchain_anthropic`, `pyyaml`, `pytest`, and `ruff` are NOT installed in this sandbox, and host installs are blocked by the repo's `block_host_installs.py` hook. Do NOT `pip install`. Any step below that says "Run: `pytest ...`" will fail at import/collection here — instead, **hand-trace** the test against the implementation, state in your report that verification was a hand-trace, and say so in the commit message body. This is how every phase since Phase 5 has worked.

---

### Task 1: `OpenAICompatibleProviderConfig` schema

**Files:**
- Modify: `services/orchestrator/model_router/config.py` (add class after `AnthropicProviderConfig`, ~line 30; extend union at lines 33-36)
- Test: `services/orchestrator/model_router/tests/unit/test_config.py`

**Interfaces:**
- Consumes: existing `Capability` enum (`services/orchestrator/model_router/capabilities.py`: `tool_calling`, `long_context`, `vision`), existing `ProviderConfig` union.
- Produces: `class OpenAICompatibleProviderConfig(BaseModel)` with fields `type: Literal["openai_compatible"]`, `model: str`, `base_url: str`, `capabilities: list[Capability]`, `cost_per_1m_input_tokens: float`, `cost_per_1m_output_tokens: float` — importable from `services.orchestrator.model_router.config`, member of the `ProviderConfig` discriminated union. Task 2 and 3 rely on the exact class name and the `"openai_compatible"` discriminator string.

- [ ] **Step 1: Write the failing tests**

Append to `services/orchestrator/model_router/tests/unit/test_config.py`, and add `OpenAICompatibleProviderConfig` to the existing `from services.orchestrator.model_router.config import (...)` block at the top of the file:

```python
def test_openai_compatible_provider_block_parses():
    raw = {
        "roles": {"coder": {"requires": ["tool_calling"], "prefer": ["local"]}},
        "providers": {
            "local": {
                "type": "openai_compatible",
                "model": "qwen/qwen3-coder-30b",
                "base_url": "http://192.168.1.50:1234/v1",
                "capabilities": ["tool_calling"],
                "cost_per_1m_input_tokens": 0,
                "cost_per_1m_output_tokens": 0,
            }
        },
    }

    config = ModelsConfig.model_validate(raw)

    assert isinstance(config.providers["local"], OpenAICompatibleProviderConfig)
    assert config.providers["local"].base_url == "http://192.168.1.50:1234/v1"


def test_openai_compatible_provider_missing_base_url_raises_validation_error():
    raw = {
        "roles": {"coder": {"requires": [], "prefer": ["local"]}},
        "providers": {
            "local": {
                "type": "openai_compatible",
                "model": "qwen/qwen3-coder-30b",
                "capabilities": [],
                "cost_per_1m_input_tokens": 0,
                "cost_per_1m_output_tokens": 0,
            }
        },
    }

    with pytest.raises(ValidationError):
        ModelsConfig.model_validate(raw)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest services/orchestrator/model_router/tests/unit/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'OpenAICompatibleProviderConfig'`.
Sandbox: collection fails on `import yaml` instead — hand-trace: confirm the name does not yet exist in `config.py`.

- [ ] **Step 3: Implement the schema**

In `services/orchestrator/model_router/config.py`, insert after `AnthropicProviderConfig` and update the union:

```python
class OpenAICompatibleProviderConfig(BaseModel):
    """Config for a provider speaking the OpenAI chat-completions API.

    Covers any self-hosted OpenAI-compatible server -- LM Studio, vLLM,
    llama.cpp server -- typically running on a separate machine from the
    Foundry host, reached via base_url.
    """

    type: Literal["openai_compatible"]
    model: str
    base_url: str
    capabilities: list[Capability] = Field(default_factory=list)
    cost_per_1m_input_tokens: float = Field(ge=0)
    cost_per_1m_output_tokens: float = Field(ge=0)


ProviderConfig = Annotated[
    OllamaProviderConfig | AnthropicProviderConfig | OpenAICompatibleProviderConfig,
    Field(discriminator="type"),
]
```

(The `ProviderConfig` assignment replaces the existing one at lines 33-36.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/model_router/tests/unit/test_config.py -v`
Expected: PASS (all tests, including pre-existing ones — the union change must not break `ollama`/`anthropic` parsing).
Sandbox: hand-trace — walk both new tests through the Pydantic discriminated union; confirm the discriminator string matches, `base_url` is required (no default), and existing tests' payloads still select their original classes.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/model_router/config.py services/orchestrator/model_router/tests/unit/test_config.py
git commit -m "feat(router): openai_compatible provider config schema"
```

---

### Task 2: Router constructs throttled `ChatOpenAI` for `openai_compatible` providers

**Files:**
- Modify: `services/orchestrator/model_router/router.py` (imports ~line 9, new constant ~line 18, constructor loop at lines 129-148, class docstring)
- Modify: `pyproject.toml` (dependency list, after `"langchain-ollama>=0.2",` at line 16)
- Test: `services/orchestrator/model_router/tests/unit/test_router.py`

**Interfaces:**
- Consumes: `OpenAICompatibleProviderConfig` from Task 1 (discriminator `"openai_compatible"`, field `base_url: str`); existing `_ThrottledChatModel(model, semaphore)`, `PROVIDER_MAX_RETRIES = 5`.
- Produces: `ModelRouter` resolves `openai_compatible` providers to a `_ThrottledChatModel` wrapping `ChatOpenAI`; module constant `OPENAI_COMPATIBLE_PLACEHOLDER_API_KEY = "not-needed"`. Constructed **whenever declared in config** — no env gate, no `anthropic_api_key` requirement. Each `openai_compatible` provider gets its **own** `threading.Semaphore(1)` (not the shared `_local_semaphore`).

- [ ] **Step 1: Add the dependency**

In `pyproject.toml` `dependencies`, after `"langchain-ollama>=0.2",` add:

```toml
    "langchain-openai>=0.2",
```

(Do not `pip install` in the sandbox — the dependency takes effect in the real deployment's image build.)

- [ ] **Step 2: Write the failing tests**

Append to `services/orchestrator/model_router/tests/unit/test_router.py`. Add `from langchain_openai import ChatOpenAI` to the imports at the top of the file.

```python
def test_router_constructs_openai_compatible_model_with_configured_endpoint():
    from services.orchestrator.model_router.router import _ThrottledChatModel

    config = _config(
        {
            "roles": {"coder": {"requires": ["tool_calling"], "prefer": ["local"]}},
            "providers": {
                "local": {
                    "type": "openai_compatible",
                    "model": "qwen/qwen3-coder-30b",
                    "base_url": "http://192.168.1.50:1234/v1",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                }
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key=None)

    model = router.resolve("coder")

    assert isinstance(model, _ThrottledChatModel)
    assert isinstance(model._model, ChatOpenAI)
    assert model.openai_api_base == "http://192.168.1.50:1234/v1"
    assert model.model_name == "qwen/qwen3-coder-30b"
    assert model.max_retries == PROVIDER_MAX_RETRIES


def test_openai_compatible_provider_needs_no_anthropic_api_key():
    config = _config(
        {
            "roles": {"coder": {"requires": [], "prefer": ["local"]}},
            "providers": {
                "local": {
                    "type": "openai_compatible",
                    "model": "qwen/qwen3-coder-30b",
                    "base_url": "http://192.168.1.50:1234/v1",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                }
            },
        }
    )

    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key=None)

    assert "local" in router.configured_providers()


def test_openai_compatible_providers_get_independent_semaphores():
    """Each remote endpoint is its own GPU: remote providers must not share a
    semaphore with each other or with the in-host Ollama semaphore, while
    every call to one remote provider still serializes through its own."""
    config = _config(
        {
            "roles": {
                "role_a": {"requires": [], "prefer": ["remote_a"]},
                "role_b": {"requires": [], "prefer": ["remote_b"]},
                "role_c": {"requires": [], "prefer": ["local"]},
            },
            "providers": {
                "remote_a": {
                    "type": "openai_compatible",
                    "model": "qwen/qwen3-coder-30b",
                    "base_url": "http://192.168.1.50:1234/v1",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                },
                "remote_b": {
                    "type": "openai_compatible",
                    "model": "qwen/qwen3-8b",
                    "base_url": "http://192.168.1.60:1234/v1",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                },
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                },
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=True, anthropic_api_key=None)

    semaphore_a = router.resolve("role_a")._semaphore
    semaphore_b = router.resolve("role_b")._semaphore
    semaphore_ollama = router.resolve("role_c")._semaphore

    assert semaphore_a is not semaphore_b
    assert semaphore_a is not semaphore_ollama
    assert semaphore_b is not semaphore_ollama
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest services/orchestrator/model_router/tests/unit/test_router.py -v`
Expected: FAIL — the router's current `else` branch treats `openai_compatible` as anthropic and raises `ValueError: provider 'local' requires an anthropic_api_key` (or `ImportError` on `langchain_openai` if the constructor branch doesn't exist).
Sandbox: hand-trace — confirm `router.py` lines 139-148 would hit the `anthropic_api_key is None` `ValueError` for a non-ollama provider.

- [ ] **Step 4: Implement the router branch**

In `services/orchestrator/model_router/router.py`:

Add to imports (after `from langchain_ollama import ChatOllama`):

```python
from langchain_openai import ChatOpenAI
```

Add after `PROVIDER_MAX_RETRIES = 5`:

```python
OPENAI_COMPATIBLE_PLACEHOLDER_API_KEY = "not-needed"
"""Sent to openai_compatible servers, which ignore authentication (LM Studio,
vLLM without --api-key): the OpenAI client requires a non-empty key even when
the server never checks it. Not a secret. Servers requiring real
authentication are out of scope (see the 2026-07-25 spec)."""
```

Replace the constructor's provider loop (currently lines 129-148) with:

```python
        for provider_name, provider_config in config.providers.items():
            if provider_config.type == "ollama":
                if not ollama_enabled:
                    continue
                raw_model = ChatOllama(
                    model=provider_config.model,
                    base_url=provider_config.base_url,
                    max_retries=PROVIDER_MAX_RETRIES,
                )
                self._models[provider_name] = _ThrottledChatModel(raw_model, self._local_semaphore)
            elif provider_config.type == "openai_compatible":
                raw_model = ChatOpenAI(
                    model=provider_config.model,
                    base_url=provider_config.base_url,
                    api_key=OPENAI_COMPATIBLE_PLACEHOLDER_API_KEY,
                    max_retries=PROVIDER_MAX_RETRIES,
                )
                self._models[provider_name] = _ThrottledChatModel(
                    raw_model, threading.Semaphore(1)
                )
            else:
                if anthropic_api_key is None:
                    raise ValueError(
                        f"provider '{provider_name}' requires an anthropic_api_key"
                    )
                self._models[provider_name] = ChatAnthropic(
                    model=provider_config.model,
                    api_key=anthropic_api_key,
                    max_retries=PROVIDER_MAX_RETRIES,
                )
```

Update the `ModelRouter` class docstring: after the sentence ending "attempt a network call to an Ollama instance that isn't running.", add:

```
    "openai_compatible" providers (remote OpenAI-API servers such as LM
    Studio -- ADR-048) have no construction-time gate: declaring one in the
    loaded config is the enable switch, and an unreachable server surfaces
    as a call-time failure after retries, like any remote-provider outage.
    Each is throttled by its own per-provider semaphore rather than the
    shared Ollama one -- each remote endpoint is its own GPU, so two remote
    providers must not serialize against each other, but calls to one
    endpoint still never overlap.
```

Also update the `_ThrottledChatModel` docstring's first paragraph sentence "ModelRouter wraps every Ollama-backed model in this class" to reflect both uses — replace:

```
    Local inference shares one physical GPU (TODO.md Phase 18), so
    ModelRouter wraps every Ollama-backed model in this class before
    storing it; ChatAnthropic entries are stored unwrapped, so
    Claude-API-bound work runs in parallel without this constraint.
```

with:

```
    Local inference shares one physical GPU (TODO.md Phase 18), so
    ModelRouter wraps every Ollama-backed model in this class (all sharing
    one semaphore) and every openai_compatible-backed model (one semaphore
    per provider, ADR-048) before storing it; ChatAnthropic entries are
    stored unwrapped, so Claude-API-bound work runs in parallel without
    this constraint.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest services/orchestrator/model_router/tests/unit/test_router.py -v`
Expected: PASS — all new and pre-existing tests. `test_every_constructed_model_has_retries_enabled` still passes because the default `config/models.yaml` is unchanged by this task.
Sandbox: hand-trace all three new tests through the new branch, and re-trace `test_resolve_falls_back_when_preferred_provider_disabled` + `test_router_requires_anthropic_api_key_when_anthropic_provider_declared` to confirm the `elif` doesn't change ollama/anthropic behavior. Note: `ChatOpenAI(base_url=...)` is exposed as the `openai_api_base` attribute and `model` as `model_name` (langchain-openai field aliases) — that is what the endpoint test asserts through the wrapper's `__getattr__` delegation.

- [ ] **Step 6: Run ruff**

Run: `ruff check services/orchestrator/model_router/ && ruff format --check services/orchestrator/model_router/`
Expected: clean.
Sandbox: ruff unavailable — visually verify 100-char lines, import order (`langchain_openai` sorts after `langchain_ollama`), no inline comments.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml services/orchestrator/model_router/router.py services/orchestrator/model_router/tests/unit/test_router.py
git commit -m "feat(router): construct throttled ChatOpenAI for openai_compatible providers"
```

---

### Task 3: `config/models.remote-llm.yaml` preset

**Files:**
- Create: `config/models.remote-llm.yaml`
- Modify: `config/models.yaml` (header comment only, lines 1-3)
- Test: `services/orchestrator/model_router/tests/unit/test_config.py`

**Interfaces:**
- Consumes: `OpenAICompatibleProviderConfig` (Task 1) via `load_models_config()`.
- Produces: a valid preset file; deployments activate it with `cp config/models.remote-llm.yaml config/models.yaml` (the documented preset mechanism) and then edit the two placeholder values.

- [ ] **Step 1: Write the failing test**

Append to `services/orchestrator/model_router/tests/unit/test_config.py`:

```python
def test_remote_llm_models_yaml_is_valid_and_local_is_openai_compatible():
    config = load_models_config(REPO_ROOT / "config" / "models.remote-llm.yaml")

    assert isinstance(config.providers["local"], OpenAICompatibleProviderConfig)
    default_config = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert set(config.roles) == set(default_config.roles)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest services/orchestrator/model_router/tests/unit/test_config.py::test_remote_llm_models_yaml_is_valid_and_local_is_openai_compatible -v`
Expected: FAIL — `FileNotFoundError` (preset doesn't exist yet).
Sandbox: self-evident; note it in the report.

- [ ] **Step 3: Create the preset**

Create `config/models.remote-llm.yaml`:

```yaml
# Remote-LLM deployment preset (ADR-048): the GPU lives on another machine
# running an OpenAI-compatible server (LM Studio, vLLM, llama.cpp server),
# so no Ollama container runs on the Foundry host. To use it:
#   1. cp config/models.remote-llm.yaml config/models.yaml
#   2. Edit base_url to your GPU machine's LAN address (LM Studio default
#      port is 1234) and model to the id your server reports on /v1/models.
#   3. ./scripts/start.sh   -- WITHOUT --local-llm; OLLAMA_ENABLED stays unset.
# Declaring the provider is the enable switch: if the server is unreachable,
# calls fail after retries like any remote-provider outage.
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
    prefer: [claude_sonnet] # local models lack reliable vision; documented, not hidden
  memory_curator:
    requires: []
    prefer: [local, claude_haiku]

providers:
  local:
    type: openai_compatible
    base_url: http://192.168.1.50:1234/v1
    model: qwen/qwen3-coder-30b
    capabilities: [tool_calling]
    # Self-hosted -- no per-token billing.
    cost_per_1m_input_tokens: 0
    cost_per_1m_output_tokens: 0
  claude_opus:
    type: anthropic
    model: claude-opus-4-8
    capabilities: [tool_calling, long_context, vision]
    # Illustrative pricing (USD per 1M tokens) -- confirm against the live
    # Anthropic pricing page before relying on this for real budget alerts.
    cost_per_1m_input_tokens: 15.0
    cost_per_1m_output_tokens: 75.0
  claude_sonnet:
    type: anthropic
    model: claude-sonnet-5
    capabilities: [tool_calling, long_context, vision]
    cost_per_1m_input_tokens: 3.0
    cost_per_1m_output_tokens: 15.0
  claude_haiku:
    type: anthropic
    model: claude-haiku-4-5
    capabilities: [tool_calling]
    cost_per_1m_input_tokens: 0.8
    cost_per_1m_output_tokens: 4.0
```

Then update `config/models.yaml`'s header comment (lines 1-3) to mention both presets — replace:

```yaml
# Default model routing config: local-first for cost, Claude API as the
# fallback/escalation tier. See docs/model-router.md for how to add a new
# provider or role, and config/models.claude-only.yaml for the GPU-less preset.
```

with:

```yaml
# Default model routing config: local-first for cost, Claude API as the
# fallback/escalation tier. See docs/model-router.md for how to add a new
# provider or role; config/models.claude-only.yaml for the GPU-less preset;
# config/models.remote-llm.yaml for a GPU on another machine (LM Studio etc.).
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest services/orchestrator/model_router/tests/unit/test_config.py -v`
Expected: PASS — including `test_default_models_yaml_is_valid` (default file's providers untouched).
Sandbox: hand-trace — check the preset's role set matches `models.yaml`'s exactly (`planner`, `coder`, `reviewer`, `frontend_reviewer`, `memory_curator`), every `prefer` name exists under `providers:`, and the `local` block carries every `OpenAICompatibleProviderConfig` required field.

- [ ] **Step 5: Commit**

```bash
git add config/models.remote-llm.yaml config/models.yaml services/orchestrator/model_router/tests/unit/test_config.py
git commit -m "feat(router): remote-llm models.yaml preset for LAN OpenAI-compatible servers"
```

---

### Task 4: Docs — `model-router.md` + ADR-048

**Files:**
- Modify: `docs/model-router.md` (intro lines 3-7, "Adding a new provider" closing paragraph at lines 52-55, new preset section after "The Claude-only preset")
- Modify: `ARCHITECTURE.md` (append one row to the §10 Decision Log table, after row 047 at line 589)

**Interfaces:**
- Consumes: everything Tasks 1-3 produced (names must match exactly: `openai_compatible`, `OpenAICompatibleProviderConfig`, `OPENAI_COMPATIBLE_PLACEHOLDER_API_KEY`, `config/models.remote-llm.yaml`).
- Produces: docs only; no code.

- [ ] **Step 1: Update `docs/model-router.md`**

Replace the intro paragraph's parenthetical "(a `ChatAnthropic` or `ChatOllama` instance)" with "(a `ChatAnthropic`, `ChatOllama`, or `ChatOpenAI` instance)".

Replace the closing paragraph of "Adding a new provider" (lines 52-55, "Adding a genuinely new provider *type* ...") with:

```markdown
Three provider *types* exist: `ollama` (in-host Ollama container),
`anthropic` (Claude API), and `openai_compatible` (any server speaking the
OpenAI chat-completions API -- LM Studio, vLLM, llama.cpp server -- ADR-048).
`openai_compatible` providers need a `base_url`; the router sends a fixed
placeholder API key (`OPENAI_COMPATIBLE_PLACEHOLDER_API_KEY`), so servers
that enforce real authentication are not yet supported. Adding a fourth
type needs code: construct the matching LangChain chat model class in
`ModelRouter.__init__`, and add a matching `TypeProviderConfig` to the
discriminated union in `config.py`.
```

Add after the "The Claude-only preset" section:

````markdown
## The remote-LLM preset

`config/models.remote-llm.yaml` is the preset for a deployment whose GPU
lives on a different machine running an OpenAI-compatible server (LM
Studio's local server, vLLM, llama.cpp server). The `local` provider is an
`openai_compatible` entry pointing at that machine's LAN address instead of
an in-host Ollama container:

```bash
cp config/models.remote-llm.yaml config/models.yaml
# then edit base_url (GPU machine's LAN IP; LM Studio defaults to port
# 1234) and model (the id your server reports on /v1/models)
./scripts/start.sh   # WITHOUT --local-llm -- nothing is downloaded
```

Unlike `ollama`-type providers (gated by `OLLAMA_ENABLED`, which the
`--local-llm` Compose overlay sets), an `openai_compatible` provider has no
construction-time gate: declaring it in the active config is the enable
switch. If the server is unreachable, calls fail after the standard retries
(ADR-025) and surface like any provider outage -- there is no silent
mid-run fallback to Claude. Calls to each remote provider are serialized
(one in flight at a time) by a per-provider semaphore, on the same
one-GPU-per-endpoint reasoning as Phase 18's shared Ollama throttle.
````

- [ ] **Step 2: Append ADR-048 to `ARCHITECTURE.md` §10**

Append this row directly after row 047 (line 589):

```markdown
| 048 | Third provider type `openai_compatible`: `ChatOpenAI` (langchain-openai) pointed at a configured `base_url`, throttle-wrapped with a per-provider semaphore; constructed whenever declared in the loaded config — declaration is the enable switch, no env gate; fixed placeholder API key (authenticated endpoints out of scope); ships with the `config/models.remote-llm.yaml` preset | The deployment's GPU may live on another machine (e.g. an RTX 3090 PC running LM Studio) where `--local-llm`'s in-host Ollama container is useless; the OpenAI chat-completions API covers every common self-hosted server (LM Studio, vLLM, llama.cpp), so one generic type beats per-vendor types (YAGNI). ADR-015's construction-time gate maps to a Compose profile that doesn't exist for a remote server — the config file is the only honest availability signal, and an unreachable server fails after ADR-025 retries exactly like a Claude outage. Per-provider semaphore rather than the shared Phase 18 one because that semaphore models one in-host GPU, while each remote endpoint is its own GPU: two endpoints must not serialize against each other, but calls to one endpoint never overlap |
```

- [ ] **Step 3: Verify docs consistency**

Check every identifier referenced in the docs exists in the code exactly as written: `OPENAI_COMPATIBLE_PLACEHOLDER_API_KEY` (router.py), `OpenAICompatibleProviderConfig` (config.py), `config/models.remote-llm.yaml` (file exists), ADR-048 referenced from router docstring (Task 2) and preset header (Task 3).

- [ ] **Step 4: Commit**

```bash
git add docs/model-router.md ARCHITECTURE.md
git commit -m "docs(router): document openai_compatible provider type (ADR-048)"
```

---

## Final verification (after all tasks)

- [ ] Run: `pytest services/orchestrator/model_router --cov=services/orchestrator/model_router --cov-report=term-missing`
  Expected: PASS, coverage ≥ 80%.
  Sandbox: not runnable (langchain/pyyaml/pytest absent) — instead re-read the full diff (`git diff HEAD~4`) and hand-verify each test against each implementation; state clearly in the final report which verifications were real runs vs. hand-traces.
- [ ] Run: `ruff check . && ruff format --check .`
  Sandbox: not runnable — visual check per Task 2 Step 6.
- [ ] The real-deployment smoke test (user-run, on the Foundry host):
  1. `cp config/models.remote-llm.yaml config/models.yaml`, edit `base_url` + `model`.
  2. Rebuild the orchestrator image (picks up `langchain-openai`), `./scripts/start.sh` without `--local-llm`.
  3. Confirm no Ollama image/model download starts, and a project's coder role produces completions served by the LM Studio machine.
