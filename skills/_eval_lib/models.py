"""Wires a real model completion function from environment configuration.

The only module in _eval_lib that touches the network. Verifying the actual
HTTP request/response handling below requires a real ANTHROPIC_API_KEY or a
running Ollama instance -- neither is available in this development
sandbox, so only default_complete_fn's provider-selection logic is covered
by automated tests (see test_models.py). _anthropic_complete/_ollama_complete
are reviewed by hand and exercised for real on an actual deployment.
"""

import base64
import os

import httpx

from skills._eval_lib.harness import CompleteFn

ANTHROPIC_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"
ANTHROPIC_MODEL_ENV_VAR = "EVAL_ANTHROPIC_MODEL"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
OLLAMA_BASE_URL_ENV_VAR = "OLLAMA_BASE_URL"
OLLAMA_MODEL_ENV_VAR = "EVAL_OLLAMA_MODEL"
DEFAULT_OLLAMA_MODEL = "qwen3-coder:30b-a3b"


class NoProviderConfiguredError(RuntimeError):
    """Raised when neither ANTHROPIC_API_KEY nor OLLAMA_BASE_URL is set."""


def _sniff_media_type(image_bytes: bytes) -> str:
    """Detects image media type from magic-number header.

    Args:
        image_bytes: Raw image bytes to sniff.

    Returns:
        The media type string ("image/png", "image/jpeg") detected from the
        magic bytes, or "image/png" as a fallback for unrecognized formats.
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return "image/png"


def _anthropic_complete(api_key: str, model: str) -> CompleteFn:
    """Builds a CompleteFn backed by the real Anthropic Messages API.

    Args:
        api_key: A Claude API key.
        model: The Claude model id to call.

    Returns:
        A CompleteFn that posts to api.anthropic.com and returns the
        response's text content, sending an image content block first
        when image_bytes is given.
    """

    def complete(system_prompt: str, user_prompt: str, image_bytes: bytes | None) -> str:
        content: str | list[dict[str, object]] = user_prompt
        if image_bytes is not None:
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _sniff_media_type(image_bytes),
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": user_prompt},
            ]
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "system": system_prompt,
                "messages": [{"role": "user", "content": content}],
            },
            timeout=60.0,
        )
        response.raise_for_status()
        return response.json()["content"][0]["text"]

    return complete


def _ollama_complete(base_url: str, model: str) -> CompleteFn:
    """Builds a CompleteFn backed by a real Ollama instance's generate API.

    Args:
        base_url: The Ollama server's base URL (e.g. http://ollama:11434).
        model: The Ollama model tag to call.

    Returns:
        A CompleteFn that posts to <base_url>/api/generate and returns the
        response's text, sending image_bytes as a base64 image when given.
    """

    def complete(system_prompt: str, user_prompt: str, image_bytes: bytes | None) -> str:
        payload: dict[str, object] = {
            "model": model,
            "system": system_prompt,
            "prompt": user_prompt,
            "stream": False,
        }
        if image_bytes is not None:
            payload["images"] = [base64.b64encode(image_bytes).decode("ascii")]
        response = httpx.post(f"{base_url}/api/generate", json=payload, timeout=120.0)
        response.raise_for_status()
        return response.json()["response"]

    return complete


def default_complete_fn() -> CompleteFn:
    """Builds a CompleteFn from environment configuration.

    Prefers ANTHROPIC_API_KEY if set; falls back to OLLAMA_BASE_URL.

    Returns:
        A CompleteFn backed by whichever provider is configured.

    Raises:
        NoProviderConfiguredError: If neither ANTHROPIC_API_KEY nor
            OLLAMA_BASE_URL is set.
    """
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV_VAR)
    if api_key:
        model = os.environ.get(ANTHROPIC_MODEL_ENV_VAR, DEFAULT_ANTHROPIC_MODEL)
        return _anthropic_complete(api_key, model)

    base_url = os.environ.get(OLLAMA_BASE_URL_ENV_VAR)
    if base_url:
        model = os.environ.get(OLLAMA_MODEL_ENV_VAR, DEFAULT_OLLAMA_MODEL)
        return _ollama_complete(base_url, model)

    raise NoProviderConfiguredError(
        f"set {ANTHROPIC_API_KEY_ENV_VAR} or {OLLAMA_BASE_URL_ENV_VAR} to run skill evals"
    )
