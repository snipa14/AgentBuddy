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


class AnthropicProviderConfig(BaseModel):
    """Config for a provider backed by the Claude API."""

    type: Literal["anthropic"]
    model: str
    capabilities: list[Capability] = Field(default_factory=list)


ProviderConfig = Annotated[
    OllamaProviderConfig | AnthropicProviderConfig,
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
