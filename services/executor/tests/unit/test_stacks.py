"""Unit tests for the stacks.yaml schema and loader (ADR-045)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from services.executor.stacks import (
    StackCheck,
    StackConfig,
    StackManifest,
    StacksConfig,
    load_stacks_config,
)


def _minimal_stack() -> dict:
    return {
        "image": "foundry/workspace-python:3.12",
        "base_image": "python:3.12-slim",
    }


def test_stack_config_minimal_validates_with_defaults():
    stack = StackConfig.model_validate(_minimal_stack())
    assert stack.apt_packages == []
    assert stack.pip_packages == []
    assert stack.pip_break_system_packages is False
    assert stack.sandbox_user_mode == "create"
    assert stack.rename_from is None
    assert stack.env == {}
    assert stack.manifests == []
    assert stack.cache_dir is None
    assert stack.check_commands == []


def test_stack_config_full_entry_validates():
    stack = StackConfig.model_validate(
        {
            **_minimal_stack(),
            "apt_packages": ["git", "curl"],
            "pip_packages": ["playwright"],
            "pip_break_system_packages": True,
            "sandbox_user_mode": "rename",
            "rename_from": "node",
            "env": {"GOMODCACHE": "/home/sandbox/go/pkg/mod"},
            "manifests": [{"file": "package.json", "install_command": ["npm", "install"]}],
            "cache_dir": "/home/sandbox/.npm",
            "check_commands": [
                {
                    "command": ["npx", "--no-install", "tsc", "--noEmit"],
                    "requires_file": "tsconfig.json",
                }
            ],
        }
    )
    assert stack.manifests[0] == StackManifest(
        file="package.json", install_command=["npm", "install"]
    )
    assert stack.check_commands[0] == StackCheck(
        command=["npx", "--no-install", "tsc", "--noEmit"],
        requires_file="tsconfig.json",
    )


def test_stack_config_rename_mode_requires_rename_from():
    with pytest.raises(ValidationError):
        StackConfig.model_validate({**_minimal_stack(), "sandbox_user_mode": "rename"})


def test_stack_config_relative_cache_dir_rejected():
    with pytest.raises(ValidationError):
        StackConfig.model_validate({**_minimal_stack(), "cache_dir": ".npm"})


def test_stack_config_empty_install_command_rejected():
    with pytest.raises(ValidationError):
        StackConfig.model_validate(
            {
                **_minimal_stack(),
                "manifests": [{"file": "go.mod", "install_command": []}],
            }
        )


def test_stack_config_empty_check_command_rejected():
    with pytest.raises(ValidationError):
        StackConfig.model_validate({**_minimal_stack(), "check_commands": [{"command": []}]})


def test_stack_config_test_command_defaults_to_none():
    stack = StackConfig.model_validate(_minimal_stack())
    assert stack.test_command is None


def test_stack_config_empty_test_command_rejected():
    with pytest.raises(ValidationError):
        StackConfig.model_validate({**_minimal_stack(), "test_command": []})


def test_stacks_config_empty_mapping_allowed():
    assert StacksConfig(stacks={}).stacks == {}


def test_load_stacks_config_round_trips(tmp_path: Path):
    pytest.importorskip("yaml")
    config_file = tmp_path / "stacks.yaml"
    config_file.write_text(
        "stacks:\n"
        "  python:\n"
        "    image: foundry/workspace-python:3.12\n"
        "    base_image: python:3.12-slim\n"
        "    cache_dir: /home/sandbox/.cache/pip\n"
    )
    loaded = load_stacks_config(config_file)
    assert set(loaded.stacks) == {"python"}
    assert loaded.stacks["python"].cache_dir == "/home/sandbox/.cache/pip"


def test_load_stacks_config_missing_file_raises(tmp_path: Path):
    pytest.importorskip("yaml")
    with pytest.raises(FileNotFoundError):
        load_stacks_config(tmp_path / "absent.yaml")
