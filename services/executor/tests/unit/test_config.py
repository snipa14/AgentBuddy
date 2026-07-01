"""Unit tests for ExecutorConfig.from_env()."""

import pytest

from services.executor.config import ExecutorConfig


def test_from_env_raises_value_error_when_projects_host_path_unset(monkeypatch):
    monkeypatch.delenv("PROJECTS_HOST_PATH", raising=False)

    with pytest.raises(ValueError):
        ExecutorConfig.from_env()


def test_from_env_uses_defaults_when_only_required_var_set(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECTS_HOST_PATH", str(tmp_path))
    monkeypatch.delenv("WORKSPACE_IMAGE", raising=False)
    monkeypatch.delenv("WORKSPACE_MEM_LIMIT", raising=False)

    config = ExecutorConfig.from_env()

    assert config.projects_host_path == tmp_path
    assert config.workspace_image == "foundry/workspace-python:3.12"
    assert config.mem_limit == "2g"
    assert config.nano_cpus == 2_000_000_000
    assert config.command_timeout_seconds == 300


def test_from_env_reads_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECTS_HOST_PATH", str(tmp_path))
    monkeypatch.setenv("WORKSPACE_IMAGE", "custom/image:1")
    monkeypatch.setenv("WORKSPACE_NETWORK", "custom_net")
    monkeypatch.setenv("WORKSPACE_MEM_LIMIT", "4g")
    monkeypatch.setenv("WORKSPACE_NANO_CPUS", "1000000000")
    monkeypatch.setenv("WORKSPACE_COMMAND_TIMEOUT_SECONDS", "60")

    config = ExecutorConfig.from_env()

    assert config.workspace_image == "custom/image:1"
    assert config.network_name == "custom_net"
    assert config.mem_limit == "4g"
    assert config.nano_cpus == 1_000_000_000
    assert config.command_timeout_seconds == 60
