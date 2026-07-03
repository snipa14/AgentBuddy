"""Unit tests for ModelsConfig loading and validation."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from services.orchestrator.model_router.config import (
    AnthropicProviderConfig,
    ModelsConfig,
    OllamaProviderConfig,
    load_models_config,
)

REPO_ROOT = Path(__file__).resolve().parents[5]

VALID_CONFIG = {
    "roles": {
        "coder": {"requires": ["tool_calling"], "prefer": ["local", "claude_sonnet"]},
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
            "capabilities": ["tool_calling", "vision"],
            "cost_per_1m_input_tokens": 3.0,
            "cost_per_1m_output_tokens": 15.0,
        },
    },
}


def test_load_models_config_valid_file_returns_models_config(tmp_path):
    config_path = tmp_path / "models.yaml"
    config_path.write_text(yaml.safe_dump(VALID_CONFIG))

    config = load_models_config(config_path)

    assert isinstance(config, ModelsConfig)
    assert isinstance(config.providers["local"], OllamaProviderConfig)
    assert isinstance(config.providers["claude_sonnet"], AnthropicProviderConfig)
    assert config.roles["coder"].prefer == ["local", "claude_sonnet"]


def test_load_models_config_missing_file_raises_file_not_found_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_models_config(tmp_path / "does_not_exist.yaml")


def test_models_config_unknown_provider_in_prefer_raises_validation_error():
    raw = {
        "roles": {"coder": {"requires": [], "prefer": ["nonexistent"]}},
        "providers": {},
    }
    with pytest.raises(ValidationError):
        ModelsConfig.model_validate(raw)


def test_role_config_empty_prefer_raises_validation_error():
    raw = {
        "roles": {"coder": {"requires": [], "prefer": []}},
        "providers": {},
    }
    with pytest.raises(ValidationError):
        ModelsConfig.model_validate(raw)


def test_provider_config_missing_cost_fields_raises_validation_error():
    raw = {
        "roles": {"coder": {"requires": [], "prefer": ["claude_sonnet"]}},
        "providers": {
            "claude_sonnet": {
                "type": "anthropic",
                "model": "claude-sonnet-5",
                "capabilities": [],
            }
        },
    }
    with pytest.raises(ValidationError):
        ModelsConfig.model_validate(raw)


def test_provider_config_unknown_type_raises_validation_error():
    raw = {
        "roles": {},
        "providers": {"mystery": {"type": "openai", "model": "gpt-x"}},
    }
    with pytest.raises(ValidationError):
        ModelsConfig.model_validate(raw)


def test_default_models_yaml_is_valid():
    config = load_models_config(REPO_ROOT / "config" / "models.yaml")
    assert "local" in config.providers
    assert "planner" in config.roles


def test_claude_only_models_yaml_is_valid_and_has_no_local_provider():
    config = load_models_config(REPO_ROOT / "config" / "models.claude-only.yaml")
    assert "local" not in config.providers
    for role in config.roles.values():
        assert all(config.providers[name].type == "anthropic" for name in role.prefer)
