"""Tests for models.default_complete_fn -- provider-selection logic only.

_anthropic_complete/_ollama_complete's actual HTTP behavior needs a real
ANTHROPIC_API_KEY or Ollama instance to test meaningfully; neither is
available in this sandbox, so they're patched out here and reviewed by hand
instead (see the module docstring).
"""

import pytest

from skills._eval_lib import models


def test_default_complete_fn_prefers_anthropic_when_api_key_set(monkeypatch):
    monkeypatch.setenv(models.ANTHROPIC_API_KEY_ENV_VAR, "sk-ant-fake")
    monkeypatch.setenv(models.OLLAMA_BASE_URL_ENV_VAR, "http://ollama:11434")
    monkeypatch.setattr(models, "_anthropic_complete", lambda key, model: "anthropic-marker")
    monkeypatch.setattr(models, "_ollama_complete", lambda url, model: "ollama-marker")

    assert models.default_complete_fn() == "anthropic-marker"


def test_default_complete_fn_falls_back_to_ollama_when_no_api_key(monkeypatch):
    monkeypatch.delenv(models.ANTHROPIC_API_KEY_ENV_VAR, raising=False)
    monkeypatch.setenv(models.OLLAMA_BASE_URL_ENV_VAR, "http://ollama:11434")
    monkeypatch.setattr(models, "_ollama_complete", lambda url, model: "ollama-marker")

    assert models.default_complete_fn() == "ollama-marker"


def test_default_complete_fn_raises_when_neither_configured(monkeypatch):
    monkeypatch.delenv(models.ANTHROPIC_API_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(models.OLLAMA_BASE_URL_ENV_VAR, raising=False)

    with pytest.raises(models.NoProviderConfiguredError):
        models.default_complete_fn()


def test_default_complete_fn_uses_model_env_var_override(monkeypatch):
    monkeypatch.setenv(models.ANTHROPIC_API_KEY_ENV_VAR, "sk-ant-fake")
    monkeypatch.setenv(models.ANTHROPIC_MODEL_ENV_VAR, "claude-opus-4-8")
    captured = {}
    monkeypatch.setattr(
        models,
        "_anthropic_complete",
        lambda key, model: captured.setdefault("model", model),
    )

    models.default_complete_fn()

    assert captured["model"] == "claude-opus-4-8"


def test_sniff_media_type_detects_png_from_magic_bytes():
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"rest of PNG data"
    assert models._sniff_media_type(png_bytes) == "image/png"


def test_sniff_media_type_detects_jpeg_from_magic_bytes():
    jpeg_bytes = b"\xff\xd8\xff" + b"rest of JPEG data"
    assert models._sniff_media_type(jpeg_bytes) == "image/jpeg"


def test_sniff_media_type_fallback_on_unrecognized_bytes():
    unrecognized = b"GIF89a" + b"other data"
    assert models._sniff_media_type(unrecognized) == "image/png"
