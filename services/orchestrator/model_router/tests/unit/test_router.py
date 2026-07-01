"""Unit tests for ModelRouter.resolve() and its prefer fallback chain (ADR-001, ADR-015)."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest

from services.orchestrator.model_router.config import ModelsConfig, load_models_config
from services.orchestrator.model_router.errors import NoCapableProviderError, UnknownRoleError
from services.orchestrator.model_router.providers.anthropic import AnthropicProvider
from services.orchestrator.model_router.providers.ollama import OllamaProvider
from services.orchestrator.model_router.router import ModelRouter

REPO_ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    return client


def _config(raw: dict) -> ModelsConfig:
    return ModelsConfig.model_validate(raw)


def test_resolve_returns_first_capable_preferred_provider(mock_client):
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
                },
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": ["tool_calling"],
                },
            },
        }
    )
    router = ModelRouter(config, client=mock_client, ollama_enabled=True, anthropic_api_key="key")

    provider = router.resolve("coder")

    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "claude_sonnet"


def test_resolve_falls_back_when_preferred_provider_disabled(mock_client):
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
                },
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": ["tool_calling"],
                },
            },
        }
    )
    router = ModelRouter(config, client=mock_client, ollama_enabled=False, anthropic_api_key="key")

    provider = router.resolve("coder")

    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "claude_sonnet"
    assert "local" not in router.configured_providers()


def test_resolve_raises_unknown_role_error_for_undeclared_role(mock_client):
    config = _config(
        {
            "roles": {"coder": {"requires": [], "prefer": ["claude_sonnet"]}},
            "providers": {
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": [],
                }
            },
        }
    )
    router = ModelRouter(config, client=mock_client, ollama_enabled=False, anthropic_api_key="key")

    with pytest.raises(UnknownRoleError):
        router.resolve("nonexistent_role")


def test_resolve_raises_no_capable_provider_error_when_requirements_unmet(mock_client):
    config = _config(
        {
            "roles": {"frontend_reviewer": {"requires": ["vision"], "prefer": ["claude_haiku"]}},
            "providers": {
                "claude_haiku": {
                    "type": "anthropic",
                    "model": "claude-haiku-4-5",
                    "capabilities": ["tool_calling"],
                }
            },
        }
    )
    router = ModelRouter(config, client=mock_client, ollama_enabled=False, anthropic_api_key="key")

    with pytest.raises(NoCapableProviderError):
        router.resolve("frontend_reviewer")


def test_resolve_raises_no_capable_provider_error_when_only_preferred_provider_is_disabled(
    mock_client,
):
    config = _config(
        {
            "roles": {"coder": {"requires": ["tool_calling"], "prefer": ["local"]}},
            "providers": {
                "local": {
                    "type": "ollama",
                    "model": "qwen3-coder",
                    "base_url": "http://ollama:11434",
                    "capabilities": ["tool_calling"],
                }
            },
        }
    )
    router = ModelRouter(config, client=mock_client, ollama_enabled=False, anthropic_api_key=None)

    with pytest.raises(NoCapableProviderError):
        router.resolve("coder")


def test_router_requires_anthropic_api_key_when_anthropic_provider_declared(mock_client):
    config = _config(
        {
            "roles": {"coder": {"requires": [], "prefer": ["claude_sonnet"]}},
            "providers": {
                "claude_sonnet": {
                    "type": "anthropic",
                    "model": "claude-sonnet-5",
                    "capabilities": [],
                }
            },
        }
    )

    with pytest.raises(ValueError):
        ModelRouter(config, client=mock_client, ollama_enabled=False, anthropic_api_key=None)


def test_router_never_constructs_ollama_provider_when_disabled(monkeypatch, mock_client):
    init_spy = Mock(wraps=OllamaProvider.__init__)
    monkeypatch.setattr(OllamaProvider, "__init__", init_spy)

    config = load_models_config(REPO_ROOT / "config" / "models.yaml")
    ModelRouter(config, client=mock_client, ollama_enabled=False, anthropic_api_key="key")

    init_spy.assert_not_called()


def test_resolve_claude_only_preset_never_uses_ollama_provider(mock_client):
    config = load_models_config(REPO_ROOT / "config" / "models.claude-only.yaml")
    router = ModelRouter(config, client=mock_client, ollama_enabled=False, anthropic_api_key="key")

    assert "local" not in router.configured_providers()
    for role in config.roles:
        provider = router.resolve(role)
        assert isinstance(provider, AnthropicProvider)


def test_router_reads_ollama_enabled_from_env_when_not_given_explicitly(monkeypatch, mock_client):
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
                }
            },
        }
    )

    router = ModelRouter(config, client=mock_client)

    assert "local" in router.configured_providers()
