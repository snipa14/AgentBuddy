"""Shared fixtures for orchestrator graph unit tests."""

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def memory_conn(tmp_path: Path) -> sqlite3.Connection:
    """A temporary in-memory global memory connection.

    The Memory Curator needs an open sqlite connection; this fixture gives
    each test its own isolated database so curation side-effects don't bleed
    between tests.
    """
    from services.orchestrator.memory.global_store import build_global_memory_connection

    return build_global_memory_connection(tmp_path / "foundry_memory.db")


@pytest.fixture()
def projects_root(tmp_path: Path) -> Path:
    """A temporary projects root directory.

    The Memory Curator writes per-project LEARNINGS.md files here; each test
    gets its own isolated temp directory.
    """
    return tmp_path / "projects"


@pytest.fixture()
def two_stack_registry():
    """A StacksConfig with two stacks, for node_static_checks tests (ADR-046).

    The "python" entry carries one real check_command (ruff) so tests can
    exercise a non-empty registry; "node" has none, standing in for a stack
    with nothing applicable to run.
    """
    from services.orchestrator.stacks import StackCheck, StackConfig, StacksConfig

    return StacksConfig(
        stacks={
            "python": StackConfig(
                image="foundry/workspace-python:3.12",
                base_image="python:3.12-slim",
                check_commands=[StackCheck(command=["ruff", "check", "."])],
            ),
            "node": StackConfig(
                image="foundry/workspace-node:20",
                base_image="node:20-slim",
            ),
        }
    )
