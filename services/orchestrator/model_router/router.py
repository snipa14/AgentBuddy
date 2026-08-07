"""Resolves agent roles to a configured, available LangChain chat model."""

import os
import threading
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from services.orchestrator.model_router.config import ModelsConfig, ProviderConfig
from services.orchestrator.model_router.errors import (
    NoCapableProviderError,
    UnknownRoleError,
)

OLLAMA_ENABLED_ENV_VAR = "OLLAMA_ENABLED"
PROVIDER_MAX_RETRIES = 5
OPENAI_COMPATIBLE_PLACEHOLDER_API_KEY = "not-needed"
"""Sent to openai_compatible servers, which ignore authentication (LM Studio,
vLLM without --api-key): the OpenAI client requires a non-empty key even when
the server never checks it. Not a secret. Servers requiring real
authentication are out of scope (see the 2026-07-25 spec)."""
ANTHROPIC_BASE_URL_PLACEHOLDER_API_KEY = "not-needed"
"""Sent to anthropic-typed providers with a `base_url` override in place of
the real anthropic_api_key. Not a secret."""


class _ThrottledChatModel:
    """Wraps a BaseChatModel so at most one call is ever in flight at a time.

    Local inference shares one physical GPU (TODO.md Phase 18), so
    ModelRouter wraps every Ollama-backed model in this class (all sharing
    one semaphore), every openai_compatible-backed model, and every
    anthropic-typed model whose `base_url` points somewhere other than the
    real Claude API (one semaphore per endpoint, keyed by `base_url`,
    ADR-048/ADR-050) before storing it; a ChatAnthropic entry with no
    `base_url` override (the real, hosted Claude API) is stored unwrapped,
    so that Claude-API-bound work runs in parallel without this constraint.

    A threading.Semaphore, not asyncio.Semaphore, is required: every
    LLM-backed graph node bridges to its one async call via its own
    asyncio.run() (ADR-019), so when two projects run concurrently each
    gets its own OS thread and its own event loop -- an asyncio.Semaphore
    scoped to one loop can't coordinate across them.

    Covers exactly the four methods anything in this codebase calls on a
    resolved model (agents/llm.py's invoke_structured, agents/coder.py's
    tool-calling loop): .ainvoke(), .astream(), .bind_tools(),
    .with_structured_output(). The latter two return a new
    _ThrottledChatModel wrapping whatever Runnable the real model's
    equivalent method returns, so throttling survives the call chains those
    two callers actually use. Deliberately duck-typed rather than a
    BaseChatModel subclass -- LangChain's BaseChatModel has several
    abstract methods (_generate/_agenerate/_stream/_astream) this wrapper
    has no need to implement, since nothing in this codebase calls anything
    beyond these four entry points on a router.resolve() result (confirmed
    by a repo-wide grep during Phase 18 planning).
    """

    def __init__(self, model: Any, semaphore: threading.Semaphore) -> None:
        self._model = model
        self._semaphore = semaphore

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        with self._semaphore:
            return await self._model.ainvoke(*args, **kwargs)

    async def astream(self, *args: Any, **kwargs: Any) -> Any:
        with self._semaphore:
            async for chunk in self._model.astream(*args, **kwargs):
                yield chunk

    def bind_tools(self, *args: Any, **kwargs: Any) -> "_ThrottledChatModel":
        return _ThrottledChatModel(self._model.bind_tools(*args, **kwargs), self._semaphore)

    def with_structured_output(self, *args: Any, **kwargs: Any) -> "_ThrottledChatModel":
        return _ThrottledChatModel(
            self._model.with_structured_output(*args, **kwargs), self._semaphore
        )

    def __getattr__(self, name: str) -> Any:
        """Delegates any attribute not defined on this wrapper to the real model.

        Covers accesses this wrapper doesn't need to intercept itself -- e.g.
        `.max_retries`, checked directly on a resolved model by
        test_every_constructed_model_has_retries_enabled -- without having to
        enumerate every such attribute by hand.
        """
        return getattr(self._model, name)


ResolvedChatModel = BaseChatModel | _ThrottledChatModel
"""What resolve() actually returns: a real BaseChatModel, or the
_ThrottledChatModel proxy wrapping one for Ollama-backed roles,
openai_compatible-backed roles, or anthropic-typed roles pointed at a
self-hosted `base_url` (ADR-050)."""


class ModelRouter:
    """Resolves agent roles to a `ResolvedChatModel`, following the config's prefer chains.

    Local provider availability is fixed once at construction time (ADR-015):
    if Ollama isn't enabled for this deployment, no `ChatOllama` is ever
    built, so resolve() can never fall through to a provider that would
    attempt a network call to an Ollama instance that isn't running.

    "openai_compatible" providers (remote OpenAI-API servers such as LM
    Studio -- ADR-048), and "anthropic"-typed providers with a `base_url`
    override (a self-hosted Anthropic-Messages-API-compatible server --
    ADR-050), have no construction-time gate: declaring one in the loaded
    config is the enable switch, and an unreachable server surfaces as a
    call-time failure after retries, like any remote-provider outage. Each
    is throttled by a per-endpoint semaphore, keyed by `base_url`, rather
    than the shared Ollama one -- each remote endpoint is its own GPU, so
    two distinct endpoints must not serialize against each other, but calls
    to one endpoint still never overlap, including when two provider
    entries share a `base_url` (e.g. two models on the same GPU server, or
    an `openai_compatible` and an `anthropic`-typed entry both pointed at
    the same LM Studio instance).

    Every constructed model has retries enabled (`max_retries`, ADR-025) --
    transient provider errors no longer need to be handled by hand-rolled
    retry logic in agent code. Every Ollama-backed model is additionally
    wrapped in `_ThrottledChatModel` so local inference never runs more than
    one call at a time (TODO.md Phase 18) -- one physical GPU serves every
    Ollama-backed role in a deployment, regardless of how many are declared
    in `models.yaml`.
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
                declares at least one "anthropic"-type provider with no
                `base_url` override (i.e. one that actually talks to the real
                Anthropic API). "anthropic"-type providers with a `base_url`
                override are self-hosted, non-Anthropic endpoints and are
                constructed with a placeholder key instead, so they never need
                this argument.

        Raises:
            ValueError: If config declares an "anthropic"-type provider with no
                `base_url` override but no anthropic_api_key was given.
        """
        if ollama_enabled is None:
            ollama_enabled = os.environ.get(OLLAMA_ENABLED_ENV_VAR, "false").lower() == "true"

        self._config = config
        self._models: dict[str, ResolvedChatModel] = {}
        self._local_semaphore = threading.Semaphore(1)
        self._remote_semaphores: dict[str, threading.Semaphore] = {}
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
                semaphore = self._remote_semaphores.setdefault(
                    provider_config.base_url, threading.Semaphore(1)
                )
                self._models[provider_name] = _ThrottledChatModel(raw_model, semaphore)
            else:
                if provider_config.base_url is None:
                    if anthropic_api_key is None:
                        raise ValueError(
                            f"provider '{provider_name}' requires an anthropic_api_key"
                        )
                    api_key = anthropic_api_key
                else:
                    api_key = ANTHROPIC_BASE_URL_PLACEHOLDER_API_KEY

                raw_model = ChatAnthropic(
                    model=provider_config.model,
                    api_key=api_key,
                    base_url=provider_config.base_url,
                    max_retries=PROVIDER_MAX_RETRIES,
                )
                if provider_config.base_url is None:
                    self._models[provider_name] = raw_model
                else:
                    semaphore = self._remote_semaphores.setdefault(
                        provider_config.base_url, threading.Semaphore(1)
                    )
                    self._models[provider_name] = _ThrottledChatModel(raw_model, semaphore)

    def configured_providers(self) -> frozenset[str]:
        """Returns the names of providers actually constructed for this deployment.

        A provider declared in models.yaml is absent from this set if it's an
        "ollama"-type provider and the local-llm profile isn't enabled.
        """
        return frozenset(self._models)

    def resolve(self, role: str) -> ResolvedChatModel:
        """Resolves a role to the first configured, capable provider in its prefer list.

        Args:
            role: A role name declared under `roles:` in models.yaml.

        Returns:
            The `BaseChatModel` for the first provider in the role's prefer
            list that was constructed (i.e. enabled) and supports every
            capability the role requires, wrapped in `_ThrottledChatModel`
            if it's an Ollama-backed provider, an openai_compatible-backed
            provider, or an anthropic-typed provider with a `base_url`
            override.

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
