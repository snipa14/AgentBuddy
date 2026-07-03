"""Resolves agent roles to a configured, available LangChain chat model."""

import os

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama

from services.orchestrator.model_router.config import ModelsConfig, ProviderConfig
from services.orchestrator.model_router.errors import (
    NoCapableProviderError,
    UnknownRoleError,
)

OLLAMA_ENABLED_ENV_VAR = "OLLAMA_ENABLED"
PROVIDER_MAX_RETRIES = 5


class ModelRouter:
    """Resolves agent roles to a `BaseChatModel`, following the config's prefer chains.

    Local provider availability is fixed once at construction time (ADR-015):
    if Ollama isn't enabled for this deployment, no `ChatOllama` is ever
    built, so resolve() can never fall through to a provider that would
    attempt a network call to an Ollama instance that isn't running.

    Every constructed model has retries enabled (`max_retries`, ADR-025) --
    transient provider errors no longer need to be handled by hand-rolled
    retry logic in agent code.
    """

    def __init__(
        self,
        config: ModelsConfig,
        ollama_enabled: bool | None = None,
        anthropic_api_key: str | None = None,
    ) -> None:
        """Builds a router from a validated config.

        Args:
            config: A validated ModelsConfig, typically from load_models_config().
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
        self._models: dict[str, BaseChatModel] = {}
        for provider_name, provider_config in config.providers.items():
            if provider_config.type == "ollama":
                if not ollama_enabled:
                    continue
                self._models[provider_name] = ChatOllama(
                    model=provider_config.model,
                    base_url=provider_config.base_url,
                    max_retries=PROVIDER_MAX_RETRIES,
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

    def configured_providers(self) -> frozenset[str]:
        """Returns the names of providers actually constructed for this deployment.

        A provider declared in models.yaml is absent from this set if it's an
        "ollama"-type provider and the local-llm profile isn't enabled.
        """
        return frozenset(self._models)

    def resolve(self, role: str) -> BaseChatModel:
        """Resolves a role to the first configured, capable provider in its prefer list.

        Args:
            role: A role name declared under `roles:` in models.yaml.

        Returns:
            The `BaseChatModel` for the first provider in the role's prefer
            list that was constructed (i.e. enabled) and supports every
            capability the role requires.

        Raises:
            UnknownRoleError: If role isn't declared in the loaded config.
            NoCapableProviderError: If no provider in the role's prefer list is
                both configured and capable of satisfying the role's requirements.
        """
        provider_name = self._resolve_provider_name(role)
        return self._models[provider_name]

    def cost_usd(self, role: str, usage: dict | None) -> float:
        """Computes the USD cost of a completion using the resolved provider's pricing.

        Args:
            role: The role that produced the completion (used to look up which
                provider, and therefore which per-token pricing, actually
                served the call -- must resolve to the same provider `resolve()`
                would have returned for this role).
            usage: A LangChain `UsageMetadata`-shaped mapping (`input_tokens`/
                `output_tokens` keys), typically `AIMessage.usage_metadata`.
                None (a provider that doesn't report usage) costs 0.0.

        Returns:
            The completion's cost in USD, computed from `config/models.yaml`'s
            `cost_per_1m_input_tokens`/`cost_per_1m_output_tokens` for the
            provider that served this role.

        Raises:
            UnknownRoleError: If role isn't declared in the loaded config.
            NoCapableProviderError: If no provider in the role's prefer list is
                both configured and capable of satisfying the role's requirements.
        """
        if usage is None:
            return 0.0
        provider_name = self._resolve_provider_name(role)
        provider_config = self._config.providers[provider_name]
        input_tokens = usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0
        return (
            input_tokens / 1_000_000 * provider_config.cost_per_1m_input_tokens
            + output_tokens / 1_000_000 * provider_config.cost_per_1m_output_tokens
        )

    def _resolve_provider_name(self, role: str) -> str:
        """Walks a role's prefer chain, returning the first configured, capable provider's name."""
        role_config = self._config.roles.get(role)
        if role_config is None:
            raise UnknownRoleError(f"role '{role}' is not defined in models.yaml")

        for provider_name in role_config.prefer:
            provider_config: ProviderConfig | None = self._config.providers.get(provider_name)
            if provider_name not in self._models or provider_config is None:
                continue
            if all(capability in provider_config.capabilities for capability in role_config.requires):
                return provider_name

        raise NoCapableProviderError(
            f"no available provider satisfies role '{role}' "
            f"(prefer={role_config.prefer!r}, requires={role_config.requires!r})"
        )
