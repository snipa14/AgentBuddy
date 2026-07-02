"""Unit tests for per-project markdown memory files."""

import pytest

from services.orchestrator.memory.project_memory import (
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
