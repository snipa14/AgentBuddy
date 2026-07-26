"""Unit tests for per-project markdown memory files."""

import pytest

from services.orchestrator.memory import project_memory
from services.orchestrator.memory.project_memory import (
    WORKSPACE_CONTAINER_UID,
    append_memory_entry,
    read_memory_file,
)


def test_read_memory_file_returns_empty_string_when_not_yet_created(tmp_path):
    assert read_memory_file(tmp_path, "proj-1", "LEARNINGS.md") == ""


def test_read_memory_file_raises_on_unknown_filename(tmp_path):
    with pytest.raises(ValueError, match="unknown memory file"):
        read_memory_file(tmp_path, "proj-1", "NOTES.md")


def test_append_memory_entry_raises_on_unknown_filename(tmp_path):
    with pytest.raises(ValueError, match="unknown memory file"):
        append_memory_entry(tmp_path, "proj-1", "NOTES.md", "hello")


def test_append_memory_entry_creates_directory_and_file(tmp_path):
    append_memory_entry(tmp_path, "proj-1", "LEARNINGS.md", "first entry")

    assert read_memory_file(tmp_path, "proj-1", "LEARNINGS.md") == "first entry\n"


def test_append_memory_entry_adds_trailing_newline_when_missing(tmp_path):
    append_memory_entry(tmp_path, "proj-1", "DECISIONS.md", "no trailing newline")

    assert read_memory_file(tmp_path, "proj-1", "DECISIONS.md").endswith("\n")


def test_append_memory_entry_never_rewrites_existing_content(tmp_path):
    append_memory_entry(tmp_path, "proj-1", "LEARNINGS.md", "entry one")
    path = tmp_path / "proj-1" / "memory" / "LEARNINGS.md"
    bytes_after_first_write = path.read_bytes()

    append_memory_entry(tmp_path, "proj-1", "LEARNINGS.md", "entry two")

    bytes_after_second_write = path.read_bytes()
    assert bytes_after_second_write.startswith(bytes_after_first_write)
    assert bytes_after_second_write == bytes_after_first_write + b"entry two\n"


def test_projects_are_isolated_from_each_other(tmp_path):
    append_memory_entry(tmp_path, "proj-a", "LEARNINGS.md", "only in a")

    assert read_memory_file(tmp_path, "proj-b", "LEARNINGS.md") == ""


def test_append_memory_entry_chowns_the_project_dir_to_the_workspace_container_uid(
    tmp_path, monkeypatch
):
    """The Orchestrator runs as root (no USER in its Dockerfile), so the
    project directory this call creates would otherwise be root-owned at
    755 -- blocking the Executor's non-root workspace container (fixed UID,
    services/executor/containers.py's WORKSPACE_UID) from writing anything
    into it. create()/start() succeed regardless, but every write_file's
    mkdir -p then fails with a permission error, observed in production as
    every write_file 500ing while read_file/run_command kept succeeding.
    os.chown is patched here rather than asserting real ownership, since a
    non-root test runner can't chown to an arbitrary UID -- os.geteuid is
    also patched to simulate running as root, the only way the real chown
    call is ever reached."""
    chown_calls = []
    monkeypatch.setattr(project_memory.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        project_memory.os, "chown", lambda path, uid, gid: chown_calls.append((path, uid, gid))
    )

    append_memory_entry(tmp_path, "proj-1", "LEARNINGS.md", "hello")

    assert len(chown_calls) == 1
    path, uid, gid = chown_calls[0]
    assert path == tmp_path / "proj-1"
    assert uid == WORKSPACE_CONTAINER_UID
    assert gid == WORKSPACE_CONTAINER_UID


def test_append_memory_entry_skips_chown_when_not_running_as_root(tmp_path, monkeypatch):
    """A non-root dev/test process can't chown to an arbitrary UID -- this
    must not raise PermissionError and break local development or CI, which
    is also why every other test in this file works unmodified: they all
    run as whatever non-root UID the test runner actually is."""
    chown_calls = []
    monkeypatch.setattr(project_memory.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        project_memory.os, "chown", lambda path, uid, gid: chown_calls.append((path, uid, gid))
    )

    append_memory_entry(tmp_path, "proj-1", "LEARNINGS.md", "hello")

    assert chown_calls == []
