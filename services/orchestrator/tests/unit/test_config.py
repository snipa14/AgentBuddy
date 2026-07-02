"""Unit tests for OrchestratorConfig.from_env."""

from pathlib import Path

from services.orchestrator.config import (
    DEFAULT_EXECUTOR_URL,
    DEFAULT_MEMORY_DB_PATH,
    OrchestratorConfig,
)


def test_from_env_uses_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("MEMORY_DB_PATH", raising=False)
    monkeypatch.delenv("EXECUTOR_URL", raising=False)

    config = OrchestratorConfig.from_env()

    assert config.memory_db_path == DEFAULT_MEMORY_DB_PATH
    assert config.executor_url == DEFAULT_EXECUTOR_URL


def test_from_env_reads_memory_db_path_override(monkeypatch, tmp_path):
    override = tmp_path / "custom.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(override))

    config = OrchestratorConfig.from_env()

    assert config.memory_db_path == Path(override)


def test_from_env_reads_executor_url_override(monkeypatch):
    monkeypatch.setenv("EXECUTOR_URL", "http://localhost:9000")

    config = OrchestratorConfig.from_env()

    assert config.executor_url == "http://localhost:9000"
