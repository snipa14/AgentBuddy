"""Unit tests for ExecutorConfig.from_env()."""

from pathlib import Path

import pytest

from services.executor.config import ExecutorConfig, image_for_stack


def test_from_env_raises_value_error_when_projects_host_path_unset(monkeypatch):
    monkeypatch.delenv("PROJECTS_HOST_PATH", raising=False)

    with pytest.raises(ValueError):
        ExecutorConfig.from_env()


def test_from_env_uses_defaults_when_only_required_var_set(monkeypatch, tmp_path):
    pytest.importorskip("yaml")
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
    pytest.importorskip("yaml")
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


def test_from_env_treats_empty_string_env_vars_as_unset(monkeypatch, tmp_path):
    """os.environ.get(key, default) only falls back when key is entirely
    absent -- a variable explicitly set to "" (common with docker-compose
    interpolation of an unset host variable) must still fall back to the
    documented default rather than reaching int("") and raising ValueError."""
    pytest.importorskip("yaml")
    monkeypatch.setenv("PROJECTS_HOST_PATH", str(tmp_path))
    monkeypatch.setenv("WORKSPACE_IMAGE", "")
    monkeypatch.setenv("WORKSPACE_NANO_CPUS", "")
    monkeypatch.setenv("WORKSPACE_COMMAND_TIMEOUT_SECONDS", "")

    config = ExecutorConfig.from_env()

    assert config.workspace_image == "foundry/workspace-python:3.12"
    assert config.nano_cpus == 2_000_000_000
    assert config.command_timeout_seconds == 300


def test_image_for_stack_reads_registry(two_stack_registry):
    config = ExecutorConfig(
        projects_host_path=Path("/srv/projects"), stacks=two_stack_registry
    )
    assert image_for_stack("node", config) == "foundry/workspace-node:20"


def test_image_for_stack_unknown_stack_falls_back_to_default(two_stack_registry):
    config = ExecutorConfig(
        projects_host_path=Path("/srv/projects"), stacks=two_stack_registry
    )
    assert image_for_stack("cobol", config) == config.workspace_image


def test_config_defaults_to_empty_registry():
    config = ExecutorConfig(projects_host_path=Path("/srv/projects"))
    assert config.stacks.stacks == {}


def test_from_env_reads_stacks_config_path(monkeypatch, tmp_path):
    pytest.importorskip("yaml")
    stacks_file = tmp_path / "stacks.yaml"
    stacks_file.write_text(
        "stacks:\n"
        "  python:\n"
        "    image: foundry/workspace-python:3.12\n"
        "    base_image: python:3.12-slim\n"
    )
    monkeypatch.setenv("PROJECTS_HOST_PATH", "/srv/projects")
    monkeypatch.setenv("STACKS_CONFIG_PATH", str(stacks_file))
    config = ExecutorConfig.from_env()
    assert set(config.stacks.stacks) == {"python"}
