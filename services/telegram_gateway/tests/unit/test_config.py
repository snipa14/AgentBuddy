"""Unit tests for GatewayConfig.from_env()."""

import pytest

from services.telegram_gateway.config import GatewayConfig


def test_from_env_reads_secrets_and_env(tmp_path, monkeypatch):
    (tmp_path / "telegram_bot_token").write_text("123:ABC\n")
    (tmp_path / "telegram_allowed_user_id").write_text("42\n")
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orchestrator:8000")

    config = GatewayConfig.from_env(secrets_dir=tmp_path)

    assert config.bot_token == "123:ABC"
    assert config.allowed_user_id == 42
    assert config.orchestrator_url == "http://orchestrator:8000"


def test_from_env_raises_value_error_when_orchestrator_url_unset(tmp_path, monkeypatch):
    (tmp_path / "telegram_bot_token").write_text("123:ABC")
    (tmp_path / "telegram_allowed_user_id").write_text("42")
    monkeypatch.delenv("ORCHESTRATOR_URL", raising=False)

    with pytest.raises(ValueError):
        GatewayConfig.from_env(secrets_dir=tmp_path)


def test_from_env_raises_file_not_found_when_secret_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_URL", "http://orchestrator:8000")

    with pytest.raises(FileNotFoundError):
        GatewayConfig.from_env(secrets_dir=tmp_path)
