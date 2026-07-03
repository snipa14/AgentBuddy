"""Unit tests for the in-memory progress-chunk buffer (ADR-027, TODO.md Phase 16)."""

from services.orchestrator.progress_buffer import add_progress, clear_progress, get_chunks_since


def test_get_chunks_since_returns_everything_by_default():
    add_progress("proj-1", "hello ")
    add_progress("proj-1", "world")

    chunks = get_chunks_since("proj-1")

    assert [(c.seq, c.text) for c in chunks] == [(0, "hello "), (1, "world")]


def test_get_chunks_since_excludes_already_seen_sequence_numbers():
    add_progress("proj-2", "a")
    add_progress("proj-2", "b")
    add_progress("proj-2", "c")

    chunks = get_chunks_since("proj-2", since_seq=0)

    assert [(c.seq, c.text) for c in chunks] == [(1, "b"), (2, "c")]


def test_get_chunks_since_unknown_project_returns_empty_list():
    assert get_chunks_since("no-such-project") == []


def test_clear_progress_discards_all_buffered_chunks():
    add_progress("proj-3", "some progress")

    clear_progress("proj-3")

    assert get_chunks_since("proj-3") == []


def test_buffers_are_independent_per_project():
    add_progress("proj-4", "x")
    add_progress("proj-5", "y")

    assert [c.text for c in get_chunks_since("proj-4")] == ["x"]
    assert [c.text for c in get_chunks_since("proj-5")] == ["y"]
