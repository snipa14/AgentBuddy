"""Unit tests for the Orchestrator's stacks.yaml loader copy (ADR-045)."""

from pathlib import Path

import pytest

from services.orchestrator.stacks import StackConfig, StacksConfig, load_stacks_config


def test_stack_config_minimal_validates():
    stack = StackConfig.model_validate(
        {"image": "foundry/workspace-go:1.22", "base_image": "golang:1.22-bookworm"}
    )
    assert stack.check_commands == []


def test_loader_matches_executor_copy_schema(tmp_path: Path):
    pytest.importorskip("yaml")
    config_file = tmp_path / "stacks.yaml"
    config_file.write_text(
        "stacks:\n"
        "  go:\n"
        "    image: foundry/workspace-go:1.22\n"
        "    base_image: golang:1.22-bookworm\n"
        "    check_commands:\n"
        "      - command: [go, vet, ./...]\n"
    )
    loaded = load_stacks_config(config_file)
    assert loaded.stacks["go"].check_commands[0].command == ["go", "vet", "./..."]


def test_both_copies_expose_identical_field_sets():
    from services.executor import stacks as executor_stacks

    assert set(StackConfig.model_fields) == set(
        executor_stacks.StackConfig.model_fields
    )
    assert set(StacksConfig.model_fields) == set(
        executor_stacks.StacksConfig.model_fields
    )
