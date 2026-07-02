"""Unit tests for OrchestratorConfig.from_env."""

from pathlib import Path

from services.orchestrator.config import DEFAULT_MEMORY_DB_PATH, OrchestratorConfig


def test_from_env_uses_default_memory_db_path_when_unset(monkeypatch):
    monkeypatch.delenv("MEMORY_DB_PATH", raising=False)

    config = OrchestratorConfig.from_env()

    assert config.memory_db_path == DEFAULT_MEMORY_DB_PATH


def test_from_env_reads_memory_db_path_override(monkeypatch, tmp_path):
    override = tmp_path / "custom.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(override))

    config = OrchestratorConfig.from_env()

    assert config.memory_db_path == Path(override)
