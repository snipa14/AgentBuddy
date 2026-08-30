"""Unit tests for render_approval()."""

from aiogram.types import InlineKeyboardMarkup

from services.telegram_gateway.rendering import (
    render_approval,
    render_project_buttons,
    render_project_list,
    render_project_status,
    render_task_progress,
)


def test_render_approval_includes_summary_text_and_three_buttons():
    text, keyboard = render_approval({"thread_id": "abc123", "summary": "Ready to merge?"})

    assert text == "Ready to merge?"
    assert isinstance(keyboard, InlineKeyboardMarkup)
    buttons = keyboard.inline_keyboard[0]
    assert [button.text for button in buttons] == ["Approve", "Reject", "Edit"]
    assert [button.callback_data for button in buttons] == [
        "approve:abc123",
        "reject:abc123",
        "edit:abc123",
    ]


def test_render_approval_falls_back_to_default_text_when_summary_missing():
    text, _ = render_approval({"thread_id": "abc123"})

    assert text == "Approval needed."


def test_render_task_progress_shows_done_count_and_in_progress_task():
    text = render_task_progress(
        {
            "done": 1,
            "total": 3,
            "tasks": [
                {"description": "First task", "status": "done", "attempts": 1},
                {"description": "Second task", "status": "in_progress", "attempts": 0},
                {"description": "Third task", "status": "pending", "attempts": 0},
            ],
        }
    )

    assert text == "1/3 tasks done\nIn progress: Second task"


def test_render_task_progress_shows_blocked_task_instead_of_in_progress():
    text = render_task_progress(
        {
            "done": 0,
            "total": 1,
            "tasks": [{"description": "Do the thing", "status": "blocked", "attempts": 3}],
        }
    )

    assert text == "0/1 tasks done\nBlocked: Do the thing"


def test_render_task_progress_shows_only_the_count_when_all_tasks_are_done():
    text = render_task_progress(
        {
            "done": 1,
            "total": 1,
            "tasks": [{"description": "Do the thing", "status": "done", "attempts": 1}],
        }
    )

    assert text == "1/1 tasks done"


def test_render_approval_shows_only_a_reply_button_for_plan_questions():
    text, keyboard = render_approval(
        {
            "thread_id": "proj-1",
            "kind": "plan_questions",
            "summary": "- Which language?\n- Which framework?",
        }
    )

    assert text == "- Which language?\n- Which framework?"
    buttons = keyboard.inline_keyboard[0]
    assert [button.text for button in buttons] == ["Reply"]
    assert [button.callback_data for button in buttons] == ["reply:proj-1"]


def test_render_approval_shows_approve_reject_edit_for_every_other_kind():
    text, keyboard = render_approval(
        {"thread_id": "proj-1", "kind": "merge_approval", "summary": "Merge to main?"}
    )

    assert text == "Merge to main?"
    buttons = keyboard.inline_keyboard[0]
    assert [button.text for button in buttons] == ["Approve", "Reject", "Edit"]


def test_render_project_status_shows_name_and_id_when_named():
    text = render_project_status(
        {"project_id": "build-me-a-cli", "name": "Build Me A Cli", "phase": "implementing"}
    )

    assert text == "Build Me A Cli (build-me-a-cli): implementing"


def test_render_project_status_falls_back_to_bare_id_when_unnamed():
    text = render_project_status({"project_id": "my-project", "name": None, "phase": "done"})

    assert text == "my-project: done"


def test_render_project_list_shows_name_and_id_when_named():
    text = render_project_list(
        [
            {"id": "build-me-a-cli", "name": "Build Me A Cli"},
            {"id": "my-project", "name": None},
        ]
    )

    assert text == "Projects:\n- Build Me A Cli (build-me-a-cli)\n- my-project"


def test_render_project_list_shows_hint_when_empty():
    assert render_project_list([]) == "No projects yet. Send /new <description> to start one."


def test_render_project_buttons_builds_one_row_per_project():
    keyboard = render_project_buttons(
        [
            {"id": "build-me-a-cli", "name": "Build Me A Cli"},
            {"id": "my-project", "name": None},
        ]
    )

    rows = keyboard.inline_keyboard
    assert [button.text for row in rows for button in row] == [
        "Build Me A Cli (build-me-a-cli)",
        "my-project",
    ]
    assert [button.callback_data for row in rows for button in row] == [
        "activate:build-me-a-cli",
        "activate:my-project",
    ]


def test_render_project_buttons_returns_none_when_empty():
    assert render_project_buttons([]) is None
