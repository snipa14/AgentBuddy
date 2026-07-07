"""Unit tests for ModelRouter.resolve()/cost_usd() and the prefer fallback chain
(ADR-001, ADR-015, ADR-025, ADR-026)."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama

from services.orchestrator.model_router.config import ModelsConfig, load_models_config
from services.orchestrator.model_router.errors import NoCapableProviderError, UnknownRoleError
from services.orchestrator.model_router.router import PROVIDER_MAX_RETRIES, ModelRouter

REPO_ROOT = Path(__file__).resolve().parents[5]


def _config(raw: dict) -> ModelsConfig:
    return ModelsConfig.model_validate(raw)


def test_resolve_returns_first_capable_preferred_provider():
    config = _config(
        {
            "roles": {
                "coder": {"requires": ["tool_calling"], "prefer": ["claude_sonnet", "local"]}
            },
            "providers": {
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                },
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                },
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=True, anthropic_api_key="key")

    model = router.resolve("coder")

    assert isinstance(model, ChatAnthropic)


def test_resolve_falls_back_when_preferred_provider_disabled():
    config = _config(
        {
            "roles": {
                "coder": {"requires": ["tool_calling"], "prefer": ["local", "claude_sonnet"]}
            },
            "providers": {
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                },
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                },
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key="key")

    model = router.resolve("coder")

    assert isinstance(model, ChatAnthropic)
    assert "local" not in router.configured_providers()


def test_resolve_raises_unknown_role_error_for_undeclared_role():
    config = _config(
        {
            "roles": {"coder": {"requires": [], "prefer": ["claude_sonnet"]}},
            "providers": {
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                }
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key="key")

    with pytest.raises(UnknownRoleError):
        router.resolve("nonexistent_role")


def test_resolve_raises_no_capable_provider_error_when_requirements_unmet():
    config = _config(
        {
            "roles": {"frontend_reviewer": {"requires": ["vision"], "prefer": ["claude_haiku"]}},
            "providers": {
                "claude_haiku": {
                    "type": "anthropic",
                    "model": "claude-haiku-4-5",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 0.8,
                    "cost_per_1m_output_tokens": 4.0,
                }
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key="key")

    with pytest.raises(NoCapableProviderError):
        router.resolve("frontend_reviewer")


def test_resolve_raises_no_capable_provider_error_when_only_preferred_provider_is_disabled():
    config = _config(
        {
            "roles": {"coder": {"requires": ["tool_calling"], "prefer": ["local"]}},
            "providers": {
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                }
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key=None)

    with pytest.raises(NoCapableProviderError):
        router.resolve("coder")


def test_router_requires_anthropic_api_key_when_anthropic_provider_declared():
    config = _config(
        {
            "roles": {"coder": {"requires": [], "prefer": ["claude_sonnet"]}},
            "providers": {
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                }
            },
        }
    )

    with pytest.raises(ValueError):
        ModelRouter(config, ollama_enabled=False, anthropic_api_key=None)


def test_router_never_constructs_ollama_model_when_disabled(monkeypatch):
    init_spy = Mock(wraps=ChatOllama.__init__)
    monkeypatch.setattr(ChatOllama, "__init__", init_spy)

    config = load_models_config(REPO_ROOT / "config" / "models.yaml")
    ModelRouter(config, ollama_enabled=False, anthropic_api_key="key")

    init_spy.assert_not_called()


def test_resolve_claude_only_preset_never_uses_ollama_model():
    config = load_models_config(REPO_ROOT / "config" / "models.claude-only.yaml")
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key="key")

    assert "local" not in router.configured_providers()
    for role in config.roles:
        model = router.resolve(role)
        assert isinstance(model, ChatAnthropic)


def test_router_reads_ollama_enabled_from_env_when_not_given_explicitly(monkeypatch):
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    config = _config(
        {
            "roles": {"coder": {"requires": [], "prefer": ["local"]}},
            "providers": {
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                }
            },
        }
    )

    router = ModelRouter(config)

    assert "local" in router.configured_providers()


def test_every_constructed_model_has_retries_enabled():
    config = load_models_config(REPO_ROOT / "config" / "models.yaml")
    router = ModelRouter(config, ollama_enabled=True, anthropic_api_key="key")

    for role in config.roles:
        model = router.resolve(role)
        assert model.max_retries == PROVIDER_MAX_RETRIES


def test_cost_usd_computes_dollar_delta_from_usage_metadata():
    config = _config(
        {
            "roles": {"coder": {"requires": [], "prefer": ["claude_sonnet"]}},
            "providers": {
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                }
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key="key")

    cost = router.cost_usd("coder", {"input_tokens": 1_000_000, "output_tokens": 1_000_000})

    assert cost == pytest.approx(18.0)


def test_cost_usd_returns_zero_when_usage_is_none():
    config = _config(
        {
            "roles": {"coder": {"requires": [], "prefer": ["claude_sonnet"]}},
            "providers": {
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                }
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key="key")

    assert router.cost_usd("coder", None) == 0.0


def test_router_wraps_ollama_model_in_throttle_but_leaves_anthropic_unwrapped():
    from services.orchestrator.model_router.router import _ThrottledChatModel

    config = _config(
        {
            "roles": {
                "local_role": {"requires": [], "prefer": ["local"]},
                "claude_role": {"requires": [], "prefer": ["claude_sonnet"]},
            },
            "providers": {
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                },
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": [],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                },
            },
        }
    )
    router = ModelRouter(config, ollama_enabled=True, anthropic_api_key="key")

    assert isinstance(router.resolve("local_role"), _ThrottledChatModel)
    assert not isinstance(router.resolve("claude_role"), _ThrottledChatModel)


def test_throttled_chat_model_serializes_concurrent_ainvoke_calls():
    """Two 'simultaneous' calls through the same throttle never overlap --
    proving the shared-GPU serialization constraint (TODO.md Phase 18).
    Uses threading (not asyncio) concurrency deliberately: every real
    LLM-backed graph node bridges to its one async call via its own
    asyncio.run(), each in its own OS thread when two projects run
    concurrently (ADR-019), so this test reproduces that shape directly.
    """
    import asyncio
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from langchain_core.messages import AIMessage

    from services.orchestrator.model_router.router import _ThrottledChatModel

    active = 0
    max_active = 0
    lock = threading.Lock()

    class _SlowFakeModel:
        async def ainvoke(self, *args, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return AIMessage(content="done")

    throttled = _ThrottledChatModel(_SlowFakeModel(), threading.Semaphore(1))

    def call():
        asyncio.run(throttled.ainvoke("hi"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(call), pool.submit(call)]
        for future in futures:
            future.result()

    assert max_active == 1


def test_cost_usd_uses_the_provider_that_would_actually_be_resolved():
    config = _config(
        {
            "roles": {"coder": {"requires": ["tool_calling"], "prefer": ["local", "claude_sonnet"]}},
            "providers": {
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 0,
                    "cost_per_1m_output_tokens": 0,
                },
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": ["tool_calling"],
                    "cost_per_1m_input_tokens": 3.0,
                    "cost_per_1m_output_tokens": 15.0,
                },
            },
        }
    )
    # local disabled -- resolve()/cost_usd() must both fall through to claude_sonnet
    router = ModelRouter(config, ollama_enabled=False, anthropic_api_key="key")

    cost = router.cost_usd("coder", {"input_tokens": 1_000_000, "output_tokens": 0})

    assert cost == pytest.approx(3.0)
