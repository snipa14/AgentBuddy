"""Unit tests for AnthropicProvider and OllamaProvider."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services.orchestrator.model_router.capabilities import Capability
from services.orchestrator.model_router.errors import ProviderRequestError
from services.orchestrator.model_router.providers.anthropic import AnthropicProvider
from services.orchestrator.model_router.providers.ollama import OllamaProvider
from services.orchestrator.model_router.schemas import CompletionMessage, CompletionRequest


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    return client


def _response(json_data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=json_data,
        request=httpx.Request("POST", "http://test.invalid"),
    )


def test_anthropic_provider_supports_returns_true_for_declared_capability(mock_client):
    provider = AnthropicProvider(
        name="claude_sonnet",
        model="claude-sonnet-5",
        api_key="test-key",
        capabilities=frozenset({Capability.TOOL_CALLING}),
        client=mock_client,
    )

    assert provider.supports(Capability.TOOL_CALLING) is True
    assert provider.supports(Capability.VISION) is False


def test_anthropic_provider_complete_returns_completion_result(mock_client):
    mock_client.post.return_value = _response(
        {
            "content": [{"type": "text", "text": "hello world"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )
    provider = AnthropicProvider(
        name="claude_sonnet",
        model="claude-sonnet-5",
        api_key="test-key",
        capabilities=frozenset(),
        client=mock_client,
    )
    request = CompletionRequest(
        messages=[
            CompletionMessage(role="system", content="be terse"),
            CompletionMessage(role="user", content="hi"),
        ]
    )

    result = asyncio.run(provider.complete(request))

    assert result.content == "hello world"
    assert result.model == "claude-sonnet-5"
    assert result.provider_name == "claude_sonnet"
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["system"] == "be terse"
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["headers"]["x-api-key"] == "test-key"


def test_anthropic_provider_complete_sends_image_content_blocks_when_images_present(mock_client):
    from services.orchestrator.model_router.schemas import ImageContent

    mock_client.post.return_value = _response(
        {"content": [{"type": "text", "text": "looks fine"}], "usage": None}
    )
    provider = AnthropicProvider(
        name="claude_sonnet",
        model="claude-sonnet-5",
        api_key="test-key",
        capabilities=frozenset({Capability.VISION}),
        client=mock_client,
    )
    request = CompletionRequest(
        messages=[
            CompletionMessage(
                role="user",
                content="does this look right?",
                images=[ImageContent(data="ZmFrZQ==", media_type="image/png")],
            )
        ]
    )

    asyncio.run(provider.complete(request))

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["messages"] == [
        {
            "role": "user",
            "content": [
                "does this look right?",
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "ZmFrZQ==",
                    },
                },
            ],
        }
    ]


def test_anthropic_provider_complete_raises_provider_request_error_on_connection_failure(
    mock_client,
):
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    provider = AnthropicProvider(
        name="claude_sonnet",
        model="claude-sonnet-5",
        api_key="test-key",
        capabilities=frozenset(),
        client=mock_client,
    )
    request = CompletionRequest(messages=[CompletionMessage(role="user", content="hi")])

    with pytest.raises(ProviderRequestError):
        asyncio.run(provider.complete(request))


def test_anthropic_provider_complete_raises_provider_request_error_on_non_2xx(mock_client):
    mock_client.post.return_value = _response({"error": "nope"}, status_code=500)
    provider = AnthropicProvider(
        name="claude_sonnet",
        model="claude-sonnet-5",
        api_key="test-key",
        capabilities=frozenset(),
        client=mock_client,
    )
    request = CompletionRequest(messages=[CompletionMessage(role="user", content="hi")])

    with pytest.raises(ProviderRequestError):
        asyncio.run(provider.complete(request))


def test_ollama_provider_supports_returns_true_for_declared_capability(mock_client):
    provider = OllamaProvider(
        name="local",
        model="qwen3-coder:30b-a3b",
        base_url="http://ollama:11434",
        capabilities=frozenset({Capability.TOOL_CALLING}),
        client=mock_client,
    )

    assert provider.supports(Capability.TOOL_CALLING) is True
    assert provider.supports(Capability.VISION) is False


def test_ollama_provider_complete_returns_completion_result(mock_client):
    mock_client.post.return_value = _response(
        {"message": {"role": "assistant", "content": "hi there"}}
    )
    provider = OllamaProvider(
        name="local",
        model="qwen3-coder:30b-a3b",
        base_url="http://ollama:11434/",
        capabilities=frozenset(),
        client=mock_client,
    )
    request = CompletionRequest(messages=[CompletionMessage(role="user", content="hi")])

    result = asyncio.run(provider.complete(request))

    assert result.content == "hi there"
    assert result.model == "qwen3-coder:30b-a3b"
    assert result.provider_name == "local"
    assert result.usage is None

    args, _ = mock_client.post.call_args
    assert args[0] == "http://ollama:11434/api/chat"


def test_ollama_provider_complete_raises_provider_request_error_on_connection_failure(
    mock_client,
):
    mock_client.post.side_effect = httpx.ConnectError("connection refused")
    provider = OllamaProvider(
        name="local",
        model="qwen3-coder:30b-a3b",
        base_url="http://ollama:11434",
        capabilities=frozenset(),
        client=mock_client,
    )
    request = CompletionRequest(messages=[CompletionMessage(role="user", content="hi")])

    with pytest.raises(ProviderRequestError):
        asyncio.run(provider.complete(request))
