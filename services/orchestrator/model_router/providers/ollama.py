"""Ollama chat API provider implementation."""

import httpx

from services.orchestrator.model_router.capabilities import Capability
from services.orchestrator.model_router.errors import ProviderRequestError
from services.orchestrator.model_router.schemas import CompletionRequest, CompletionResult


class OllamaProvider:
    """Provider backed by a local Ollama instance."""

    def __init__(
        self,
        name: str,
        model: str,
        base_url: str,
        capabilities: frozenset[Capability],
        client: httpx.AsyncClient,
    ) -> None:
        """Initializes the provider.

        Args:
            name: The provider's name as declared in models.yaml (e.g. "local").
            model: The Ollama model tag to request (e.g. "qwen3-coder:30b-a3b").
            base_url: The Ollama server's base URL (e.g. "http://ollama:11434").
            capabilities: The set of capabilities this provider satisfies.
            client: An httpx.AsyncClient used for the underlying HTTP call, injected
                so tests can substitute a mocked client instead of hitting the network.
        """
        self.name = name
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._capabilities = capabilities
        self._client = client

    def supports(self, capability: Capability) -> bool:
        """Reports whether this provider satisfies the given capability."""
        return capability in self._capabilities

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Sends a completion request to the local Ollama server and returns its result.

        Raises:
            ProviderRequestError: If the API call fails or returns a non-2xx status.
        """
        payload = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
            "stream": False,
            "options": {"temperature": request.temperature},
        }

        try:
            response = await self._client.post(f"{self._base_url}/api/chat", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderRequestError(
                f"Ollama API call failed for provider '{self.name}': {exc}"
            ) from exc

        body = response.json()
        return CompletionResult(
            content=body["message"]["content"],
            model=self._model,
            provider_name=self.name,
            usage=None,
        )
