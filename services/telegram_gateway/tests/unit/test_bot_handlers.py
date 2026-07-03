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
    handle_command,
    handle_free_text,
    handle_start,
)


def _webhook_response(project_id: str = "proj-1") -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"project_id": project_id, "phase": "intake", "pending_approval": None}
    return response


@pytest.fixture
def orchestrator():
    client = MagicMock()
    client.send_webhook = AsyncMock(return_value=_webhook_response())
    client.resume = AsyncMock()
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


def test_handle_command_forwards_to_orchestrator_and_acknowledges(message, orchestrator, poller):
    asyncio.run(handle_command(message, orchestrator, poller))

    orchestrator.send_webhook.assert_awaited_once_with(42, "/new build me a thing")
    message.answer.assert_awaited_once()


def test_handle_command_starts_progress_polling_for_the_new_project(message, orchestrator, poller):
    asyncio.run(handle_command(message, orchestrator, poller))

    poller.start.assert_called_once_with("proj-1", 7)


def test_handle_free_text_forwards_to_orchestrator(message, orchestrator, poller):
    message.text = "just chatting"

    asyncio.run(handle_free_text(message, orchestrator, poller))

    orchestrator.send_webhook.assert_awaited_once_with(42, "just chatting")
    poller.start.assert_called_once_with("proj-1", 7)


def test_handle_command_renders_an_approval_keyboard_when_a_gate_is_pending(
    message, orchestrator, poller
):
    response = MagicMock()
    response.json.return_value = {
        "project_id": "proj-1",
        "phase": "awaiting_plan_approval",
        "pending_approval": {"thread_id": "proj-1", "summary": "Ready to build?"},
    }
    orchestrator.send_webhook = AsyncMock(return_value=response)

    asyncio.run(handle_command(message, orchestrator, poller))

    args, kwargs = message.answer.call_args
    assert args[0] == "Ready to build?"
    assert "reply_markup" in kwargs


def test_handle_command_sends_plain_acknowledgment_when_no_gate_is_pending(
    message, orchestrator, poller
):
    asyncio.run(handle_command(message, orchestrator, poller))

    message.answer.assert_awaited_once_with("Got it -- working on that.")


def test_handle_approval_callback_resumes_with_parsed_decision_and_thread_id(orchestrator):
    callback = MagicMock()
    callback.data = "approve:thread-123"
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_reply_markup = AsyncMock()

    asyncio.run(handle_approval_callback(callback, orchestrator))

    orchestrator.resume.assert_awaited_once_with("thread-123", "approve")
    callback.answer.assert_awaited_once()
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_handle_approval_callback_handles_missing_message_gracefully(orchestrator):
    callback = MagicMock()
    callback.data = "reject:thread-9"
    callback.answer = AsyncMock()
    callback.message = None

    asyncio.run(handle_approval_callback(callback, orchestrator))

    orchestrator.resume.assert_awaited_once_with("thread-9", "reject")
