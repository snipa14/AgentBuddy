"""Unit tests for the Gateway's message/callback handlers.

Handlers are plain functions taking a mocked Message/CallbackQuery and a
mocked OrchestratorClient -- exactly what aiogram injects at runtime via
Dispatcher workflow data, so no live Dispatcher/Bot is needed here.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.telegram_gateway.bot import (
    handle_approval_callback,
    handle_free_text,
    handle_new,
    handle_projects,
    handle_start,
    handle_status,
)


def _webhook_response(project_id: str = "proj-1") -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "project_id": project_id,
        "phase": "intake",
        "pending_approval": None,
    }
    return response


def _status_response(status_code: int = 200, **body) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    return response


@pytest.fixture
def orchestrator():
    client = MagicMock()
    client.send_webhook = AsyncMock(return_value=_webhook_response())
    client.resume = AsyncMock(return_value=_webhook_response())
    client.get_status = AsyncMock(
        return_value=_status_response(
            project_id="proj-1", phase="implementing", pending_approval=None, task_progress=None
        )
    )
    client.get_projects = AsyncMock(
        return_value=_status_response(project_ids=["proj-1", "proj-2"])
    )
    return client


@pytest.fixture
def poller():
    return MagicMock()


@pytest.fixture
def message():
    msg = MagicMock()
    msg.from_user.id = 42
    msg.chat.id = 7
    msg.text = "/new build me a thing"
    msg.answer = AsyncMock()
    return msg


def test_handle_start_greets_without_calling_orchestrator(message):
    asyncio.run(handle_start(message))

    message.answer.assert_awaited_once()


def test_handle_new_forwards_to_orchestrator_and_acknowledges(message, orchestrator, poller):
    asyncio.run(handle_new(message, orchestrator, poller))

    orchestrator.send_webhook.assert_awaited_once_with(42, "/new build me a thing")
    message.answer.assert_awaited_once()


def test_handle_new_starts_progress_polling_for_the_new_project(message, orchestrator, poller):
    asyncio.run(handle_new(message, orchestrator, poller))

    poller.start.assert_called_once_with("proj-1", 7)


def test_handle_status_never_touches_the_webhook(message, orchestrator):
    """A status lookup must be read-only -- forwarding it to POST /webhook
    used to start a junk project (and a real Planner LLM call) per check."""
    message.text = "/status proj-1"

    asyncio.run(handle_status(message, orchestrator))

    orchestrator.send_webhook.assert_not_awaited()
    orchestrator.get_status.assert_awaited_once_with("proj-1")
    message.answer.assert_awaited_once_with("proj-1: implementing")


def test_handle_status_renders_task_progress_when_present(message, orchestrator):
    message.text = "/status proj-1"
    orchestrator.get_status = AsyncMock(
        return_value=_status_response(
            project_id="proj-1",
            phase="implementing",
            pending_approval=None,
            task_progress={
                "done": 1,
                "total": 2,
                "tasks": [
                    {"description": "First task", "status": "done", "attempts": 0},
                    {"description": "Second task", "status": "in_progress", "attempts": 0},
                ],
            },
        )
    )

    asyncio.run(handle_status(message, orchestrator))

    message.answer.assert_awaited_once_with(
        "proj-1: implementing\n1/2 tasks done\nIn progress: Second task"
    )


def test_handle_status_reports_unknown_project_on_404(message, orchestrator):
    message.text = "/status nope"
    orchestrator.get_status = AsyncMock(return_value=_status_response(status_code=404))

    asyncio.run(handle_status(message, orchestrator))

    message.answer.assert_awaited_once_with("Unknown project 'nope'. Try /projects.")


def test_handle_status_without_argument_lists_projects(message, orchestrator):
    message.text = "/status"

    asyncio.run(handle_status(message, orchestrator))

    orchestrator.get_projects.assert_awaited_once()
    text = message.answer.call_args.args[0]
    assert "Usage: /status <project_id>" in text
    assert "proj-1" in text and "proj-2" in text


def test_handle_projects_lists_known_ids(message, orchestrator):
    message.text = "/projects"

    asyncio.run(handle_projects(message, orchestrator))

    orchestrator.send_webhook.assert_not_awaited()
    message.answer.assert_awaited_once_with("Projects:\n- proj-1\n- proj-2")


def test_handle_projects_hints_new_when_empty(message, orchestrator):
    message.text = "/projects"
    orchestrator.get_projects = AsyncMock(return_value=_status_response(project_ids=[]))

    asyncio.run(handle_projects(message, orchestrator))

    message.answer.assert_awaited_once_with(
        "No projects yet. Send /new <description> to start one."
    )


def test_handle_free_text_forwards_to_orchestrator(message, orchestrator, poller):
    message.text = "just chatting"

    asyncio.run(handle_free_text(message, orchestrator, poller))

    orchestrator.send_webhook.assert_awaited_once_with(42, "just chatting")
    poller.start.assert_called_once_with("proj-1", 7)


def test_handle_new_renders_an_approval_keyboard_when_a_gate_is_pending(
    message, orchestrator, poller
):
    response = MagicMock()
    response.json.return_value = {
        "project_id": "proj-1",
        "phase": "awaiting_plan_approval",
        "pending_approval": {"thread_id": "proj-1", "summary": "Ready to build?"},
    }
    orchestrator.send_webhook = AsyncMock(return_value=response)

    asyncio.run(handle_new(message, orchestrator, poller))

    args, kwargs = message.answer.call_args
    assert args[0] == "Ready to build?"
    assert "reply_markup" in kwargs


def test_handle_new_sends_plain_acknowledgment_when_no_gate_is_pending(
    message, orchestrator, poller
):
    asyncio.run(handle_new(message, orchestrator, poller))

    message.answer.assert_awaited_once_with("Got it -- working on that.")


def test_handle_new_renders_task_progress_when_no_gate_is_pending(
    message, orchestrator, poller
):
    response = MagicMock()
    response.json.return_value = {
        "project_id": "proj-1",
        "phase": "implementing",
        "pending_approval": None,
        "task_progress": {
            "done": 1,
            "total": 3,
            "tasks": [
                {"description": "First task", "status": "done", "attempts": 1},
                {"description": "Second task", "status": "in_progress", "attempts": 0},
                {"description": "Third task", "status": "pending", "attempts": 0},
            ],
        },
    }
    orchestrator.send_webhook = AsyncMock(return_value=response)

    asyncio.run(handle_new(message, orchestrator, poller))

    message.answer.assert_awaited_once_with("1/3 tasks done\nIn progress: Second task")


def test_handle_new_falls_back_to_plain_acknowledgment_without_task_progress(
    message, orchestrator, poller
):
    """The original webhook fixture has no task_progress key at all -- confirms
    the new branch doesn't break the pre-existing no-tasks-yet case."""
    asyncio.run(handle_new(message, orchestrator, poller))

    message.answer.assert_awaited_once_with("Got it -- working on that.")


def _callback(data: str = "approve:thread-123") -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def test_handle_approval_callback_resumes_with_parsed_decision_and_thread_id(orchestrator):
    callback = _callback("approve:thread-123")

    asyncio.run(handle_approval_callback(callback, orchestrator))

    orchestrator.resume.assert_awaited_once_with("thread-123", "approve")
    callback.answer.assert_awaited_once_with("Approved.")
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_handle_approval_callback_uses_real_past_tense_for_reject(orchestrator):
    callback = _callback("reject:thread-9")

    asyncio.run(handle_approval_callback(callback, orchestrator))

    orchestrator.resume.assert_awaited_once_with("thread-9", "reject")
    callback.answer.assert_awaited_once_with("Rejected.")


def test_handle_approval_callback_pushes_the_next_gates_keyboard(orchestrator):
    """Approving the plan advances the graph straight to the merge gate; its
    approval keyboard must be pushed immediately, not stall until the user
    happens to send another message."""
    merge_gate = MagicMock()
    merge_gate.json.return_value = {
        "project_id": "proj-1",
        "phase": "awaiting_merge_approval",
        "pending_approval": {"thread_id": "proj-1", "summary": "Tests passed -- merge to main?"},
    }
    orchestrator.resume = AsyncMock(return_value=merge_gate)
    callback = _callback("approve:proj-1")

    asyncio.run(handle_approval_callback(callback, orchestrator))

    args, kwargs = callback.message.answer.call_args
    assert args[0] == "Tests passed -- merge to main?"
    assert "reply_markup" in kwargs


def test_handle_approval_callback_reports_terminal_phase_after_reject(orchestrator):
    blocked = MagicMock()
    blocked.json.return_value = {
        "project_id": "proj-1",
        "phase": "blocked",
        "pending_approval": None,
    }
    orchestrator.resume = AsyncMock(return_value=blocked)
    callback = _callback("reject:proj-1")

    asyncio.run(handle_approval_callback(callback, orchestrator))

    callback.message.answer.assert_awaited_once_with("Project proj-1 is blocked.")


def test_handle_approval_callback_handles_missing_message_gracefully(orchestrator):
    callback = MagicMock()
    callback.data = "reject:thread-9"
    callback.answer = AsyncMock()
    callback.message = None

    asyncio.run(handle_approval_callback(callback, orchestrator))

    orchestrator.resume.assert_awaited_once_with("thread-9", "reject")
