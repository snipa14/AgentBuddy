"""Unit tests for the global SQLite/FTS5 memory store."""

import pytest

from services.orchestrator.memory.global_store import (
    build_global_memory_connection,
    recall,
    recall_for_task,
    record_memory,
)


@pytest.fixture
def conn(tmp_path):
    connection = build_global_memory_connection(tmp_path / "foundry_memory.db")
    yield connection
    connection.close()


def test_recall_finds_a_memory_by_content_word(conn):
    record_memory(
        conn,
        project_id="proj-1",
        type_="fix",
        content="Off-by-one in the pagination cursor caused duplicate rows.",
        tags="pagination bug",
        created_at="2026-07-01T00:00:00Z",
    )

    results = recall(conn, "pagination")

    assert len(results) == 1
    assert results[0].content.startswith("Off-by-one")
    assert results[0].project_id == "proj-1"


def test_recall_finds_a_memory_by_tag(conn):
    record_memory(
        conn,
        project_id="proj-1",
        type_="preference",
        content="Prefers squash merges over merge commits.",
        tags="git workflow",
        created_at="2026-07-01T00:00:00Z",
    )

    results = recall(conn, "workflow")

    assert len(results) == 1


def test_recall_returns_empty_list_when_nothing_matches(conn):
    record_memory(
        conn,
        project_id="proj-1",
        type_="fix",
        content="Some unrelated fix.",
        tags="unrelated",
        created_at="2026-07-01T00:00:00Z",
    )

    assert recall(conn, "zzznomatch") == []


def test_recall_filters_by_project_id(conn):
    record_memory(
        conn,
        project_id="proj-a",
        type_="fix",
        content="Shared keyword: timeout",
        tags="",
        created_at="2026-07-01T00:00:00Z",
    )
    record_memory(
        conn,
        project_id="proj-b",
        type_="fix",
        content="Shared keyword: timeout",
        tags="",
        created_at="2026-07-01T00:00:00Z",
    )

    results = recall(conn, "timeout", project_id="proj-a")

    assert len(results) == 1
    assert results[0].project_id == "proj-a"


def test_recall_for_task_matches_lessons_by_task_words(conn):
    record_memory(
        conn,
        project_id="proj-a",
        type_="error_pattern",
        content="The auth fixture uses a shared session-scoped DB that flakes.",
        tags="auth flaky-test",
        created_at="2026-07-01T00:00:00Z",
    )

    lessons = recall_for_task(conn, "Add a POST /login endpoint with auth")

    assert lessons == ["The auth fixture uses a shared session-scoped DB that flakes."]


def test_recall_for_task_survives_fts5_hostile_text(conn):
    """Raw free text like quotes, parens, and bare operators is FTS5 MATCH
    syntax and would raise sqlite3.OperationalError if passed through
    unsanitized -- the task-description entrypoint must never crash on it."""
    record_memory(
        conn,
        project_id="proj-a",
        type_="fix",
        content="Escape quotes before writing YAML values.",
        tags="yaml quoting",
        created_at="2026-07-01T00:00:00Z",
    )

    lessons = recall_for_task(conn, 'Fix the "AND OR (NOT" *broken* yaml: quoting!')

    assert lessons == ["Escape quotes before writing YAML values."]


def test_recall_for_task_returns_empty_for_no_usable_tokens(conn):
    assert recall_for_task(conn, "!! ?? ..") == []


def test_recall_for_task_caps_the_number_of_lessons(conn):
    for index in range(8):
        record_memory(
            conn,
            project_id="proj-a",
            type_="fix",
            content=f"Lesson {index} about pagination cursors.",
            tags="pagination",
            created_at="2026-07-01T00:00:00Z",
        )

    lessons = recall_for_task(conn, "Fix pagination", limit=3)

    assert len(lessons) == 3


def test_record_memory_returns_incrementing_ids(conn):
    first_id = record_memory(
        conn, project_id=None, type_="skill_usage", content="a", tags="", created_at="t"
    )
    second_id = record_memory(
        conn, project_id=None, type_="skill_usage", content="b", tags="", created_at="t"
    )

    assert second_id == first_id + 1
