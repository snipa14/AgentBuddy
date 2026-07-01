"""Resolves agent roles to a configured, available Provider instance."""

import os

import httpx

from services.orchestrator.model_router.config import ModelsConfig
from services.orchestrator.model_router.errors import (
    NoCapableProviderError,
    UnknownRoleError,
)
from services.orchestrator.model_router.provider import Provider
from services.orchestrator.model_router.providers.anthropic import AnthropicProvider
from services.orchestrator.model_router.providers.ollama import OllamaProvider

OLLAMA_ENABLED_ENV_VAR = "OLLAMA_ENABLED"


class ModelRouter:
    """Resolves agent roles to a Provider, following the config's prefer fallback chains.

    Local provider availability is fixed once at construction time (ADR-015):
    if Ollama isn't enabled for this deployment, no OllamaProvider is ever
    built, so resolve() can never fall through to a provider that would
    attempt a network call to an Ollama instance that isn't running.
    """

    def __init__(
        self,
        config: ModelsConfig,
        client: httpx.AsyncClient,
        ollama_enabled: bool | None = None,
        anthropic_api_key: str | None = None,
    ) -> None:
        """Builds a router from a validated config.

        Args:
            config: A validated ModelsConfig, typically from load_models_config().
            client: An httpx.AsyncClient shared by every HTTP-backed provider.
            ollama_enabled: Whether the local-llm Compose profile is active for
                this deployment. Defaults to reading the OLLAMA_ENABLED env var
                if not given explicitly.
            anthropic_api_key: The Claude API key. Required only if config
                declares at least one "anthropic"-type provider.

        Raises:
            ValueError: If config declares an "anthropic"-type provider but no
                anthropic_api_key was given.
        """
        if ollama_enabled is None:
            ollama_enabled = os.environ.get(OLLAMA_ENABLED_ENV_VAR, "false").lower() == "true"

        self._config = config
        self._providers: dict[str, Provider] = {}
        for provider_name, provider_config in config.providers.items():
            if provider_config.type == "ollama":
                if not ollama_enabled:
                    continue
                self._providers[provider_name] = OllamaProvider(
                    name=provider_name,
                    model=provider_config.model,
                    base_url=provider_config.base_url,
                    capabilities=frozenset(provider_config.capabilities),
                    client=client,
                )
            else:
                if anthropic_api_key is None:
                    raise ValueError(
                        f"provider '{provider_name}' requires an anthropic_api_key"
                    )
                self._providers[provider_name] = AnthropicProvider(
                    name=provider_name,
                    model=provider_config.model,
                    api_key=anthropic_api_key,
                    capabilities=frozenset(provider_config.capabilities),
                    client=client,
                )

    def configured_providers(self) -> frozenset[str]:
        """Returns the names of providers actually constructed for this deployment.

        A provider declared in models.yaml is absent from this set if it's an
        "ollama"-type provider and the local-llm profile isn't enabled.
        """
        return frozenset(self._providers)

    def resolve(self, role: str) -> Provider:
        """Resolves a role to the first configured, capable provider in its prefer list.

        Args:
            role: A role name declared under `roles:` in models.yaml.

        Returns:
            The first provider in the role's prefer list that was constructed
            (i.e. enabled) and supports every capability the role requires.

        Raises:
            UnknownRoleError: If role isn't declared in the loaded config.
            NoCapableProviderError: If no provider in the role's prefer list is
                both configured and capable of satisfying the role's requirements.
        """
        role_config = self._config.roles.get(role)
        if role_config is None:
            raise UnknownRoleError(f"role '{role}' is not defined in models.yaml")

        for provider_name in role_config.prefer:
            provider = self._providers.get(provider_name)
            if provider is None:
                continue
            if all(provider.supports(capability) for capability in role_config.requires):
                return provider

        raise NoCapableProviderError(
            f"no available provider satisfies role '{role}' "
            f"(prefer={role_config.prefer!r}, requires={role_config.requires!r})"
        )
