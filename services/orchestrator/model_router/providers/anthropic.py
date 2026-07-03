"""Anthropic Messages API provider implementation."""

import httpx

from services.orchestrator.model_router.capabilities import Capability
from services.orchestrator.model_router.errors import ProviderRequestError
from services.orchestrator.model_router.schemas import (
    CompletionRequest,
    CompletionResult,
    ImageContent,
    TokenUsage,
)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider:
    """Provider backed by the Claude API (Anthropic Messages endpoint)."""

    def __init__(
        self,
        name: str,
        model: str,
        api_key: str,
        capabilities: frozenset[Capability],
        client: httpx.AsyncClient,
    ) -> None:
        """Initializes the provider.

        Args:
            name: The provider's name as declared in models.yaml (e.g. "claude_sonnet").
            model: The Anthropic model identifier to request (e.g. "claude-sonnet-5").
            api_key: The Claude API key, sourced from Docker secrets, never logged.
            capabilities: The set of capabilities this provider satisfies.
            client: An httpx.AsyncClient used for the underlying HTTP call, injected
                so tests can substitute a mocked client instead of hitting the network.
        """
        self.name = name
        self._model = model
        self._api_key = api_key
        self._capabilities = capabilities
        self._client = client

    def supports(self, capability: Capability) -> bool:
        """Reports whether this provider satisfies the given capability."""
        return capability in self._capabilities

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Sends a completion request to the Claude API and returns its result.

        Raises:
            ProviderRequestError: If the API call fails or returns a non-2xx status.
        """
        system, messages = _build_messages(request)
        payload: dict[str, object] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "messages": messages,
        }
        if system is not None:
            payload["system"] = system

        try:
            response = await self._client.post(
                ANTHROPIC_API_URL,
                json=payload,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": ANTHROPIC_API_VERSION,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderRequestError(
                f"Anthropic API call failed for provider '{self.name}': {exc}"
            ) from exc

        body = response.json()
        content = "".join(
            block["text"] for block in body["content"] if block.get("type") == "text"
        )
        usage = body.get("usage")
        return CompletionResult(
            content=content,
            model=self._model,
            provider_name=self.name,
            usage=TokenUsage(
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
            )
            if usage
            else None,
        )


def _image_block(img: ImageContent) -> dict[str, object]:
    """Converts an ImageContent model into Anthropic's image content block."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": img.media_type,
            "data": img.data,
        },
    }


def _build_messages(request: CompletionRequest) -> tuple[str | None, list[dict]]:
    """Builds the Anthropic API message payload from a completion request.

    Splits system messages out. A message with no images keeps its plain
    string `content` (the Anthropic Messages API accepts either a string or
    a content-block array); a message with images is upgraded to the
    multi-content block array format so the image(s) can be attached.

    Args:
        request: The completion request containing messages with optional images.

    Returns:
        A tuple of (system_prompt, formatted_messages) where system_prompt is
        None when no system message was present, and formatted_messages is a
        list ready to be JSON-serialized for the Anthropic Messages API.
    """
    system: str | None = None
    messages: list[dict] = []
    for message in request.messages:
        if message.role == "system":
            system = message.content
        elif message.images:
            content_parts: list[dict | str] = [message.content]
            content_parts.extend(_image_block(img) for img in message.images)
            messages.append({"role": message.role, "content": content_parts})
        else:
            messages.append({"role": message.role, "content": message.content})
    return system, messages
