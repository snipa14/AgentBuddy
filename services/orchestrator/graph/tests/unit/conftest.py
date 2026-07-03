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
