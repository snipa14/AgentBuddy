"""Pydantic schema and loader for config/models.yaml."""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from services.orchestrator.model_router.capabilities import Capability


class OllamaProviderConfig(BaseModel):
    """Config for a provider backed by a local Ollama instance."""

    type: Literal["ollama"]
    model: str
    base_url: str
    capabilities: list[Capability] = Field(default_factory=list)
    cost_per_1m_input_tokens: float = Field(ge=0)
    cost_per_1m_output_tokens: float = Field(ge=0)


class AnthropicProviderConfig(BaseModel):
    """Config for a provider speaking the Anthropic Messages API.

    base_url defaults to None, meaning the real hosted Claude API. Setting
    it points this provider at any other Messages-API-compatible server
    instead -- e.g. LM Studio's Anthropic-compatible endpoint, which (unlike
    its OpenAI-compatible one) surfaces a reasoning model's post-thinking
    answer in a distinct `tool_use` content block rather than folding it
    into a non-standard `reasoning_content` field with nothing left in
    `content` (ADR-050).
    """

    type: Literal["anthropic"]
    model: str
    base_url: str | None = None
    capabilities: list[Capability] = Field(default_factory=list)
    cost_per_1m_input_tokens: float = Field(ge=0)
    cost_per_1m_output_tokens: float = Field(ge=0)


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


class RoleConfig(BaseModel):
    """Config for a single agent role's capability requirements and provider preference."""

    requires: list[Capability] = Field(default_factory=list)
    prefer: list[str]

    @field_validator("prefer")
    @classmethod
    def _prefer_not_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("a role's 'prefer' list must not be empty")
        return value


class ModelsConfig(BaseModel):
    """The full, validated contents of a models.yaml file."""

    roles: dict[str, RoleConfig]
    providers: dict[str, ProviderConfig]

    @model_validator(mode="after")
    def _prefer_lists_reference_known_providers(self) -> "ModelsConfig":
        for role_name, role in self.roles.items():
            unknown = [name for name in role.prefer if name not in self.providers]
            if unknown:
                raise ValueError(f"role '{role_name}' prefers unknown provider(s): {unknown}")
        return self


def load_models_config(path: Path) -> ModelsConfig:
    """Loads and validates a models.yaml file.

    Args:
        path: Path to a YAML file matching the ModelsConfig schema.

    Returns:
        A validated ModelsConfig instance.

    Raises:
        FileNotFoundError: If path does not exist.
        pydantic.ValidationError: If the file's contents don't match the schema.
    """
    raw = yaml.safe_load(path.read_text())
    return ModelsConfig.model_validate(raw)
