"""Unit tests for the Gateway's message/callback handlers.

Handlers are plain functions taking a mocked Message/CallbackQuery and a
mocked OrchestratorClient -- exactly what aiogram injects at runtime via
Dispatcher workflow data, so no live Dispatcher/Bot is needed here.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from services.telegram_gateway.bot import (
    handle_approval_callback,
    handle_continue,
    handle_free_text,
    handle_new,
    handle_projects,
    handle_reply_callback,
    handle_start,
    handle_status,
)


def _webhook_response(project_id: str = "proj-1") -> MagicMock:
    response = MagicMock()
    response.status_code = 200
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

    orchestrator.send_webhook.assert_awaited_once_with(42, "/new build me a thing", force_new=True)
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


def test_handle_status_without_argument_shows_a_hint_on_server_error(message, orchestrator):
    """get_projects() can 5xx just like the webhook can -- FastAPI's default
    error body for that isn't the {"project_ids": [...]} shape this branch
    expects, so calling response.json() on it must not be allowed to raise
    (see _relay_webhook_result's matching >=500 guard)."""
    message.text = "/status"
    response = MagicMock()
    response.status_code = 500
    response.json.side_effect = ValueError("not JSON")
    orchestrator.get_projects = AsyncMock(return_value=response)

    asyncio.run(handle_status(message, orchestrator))

    message.answer.assert_awaited_once()
    response.json.assert_not_called()


def test_handle_status_shows_a_hint_on_server_error(message, orchestrator):
    """get_status() can 5xx too -- the final response.json() call must be
    guarded the same way the 404 branch already is."""
    message.text = "/status proj-1"
    response = MagicMock()
    response.status_code = 500
    response.json.side_effect = ValueError("not JSON")
    orchestrator.get_status = AsyncMock(return_value=response)

    asyncio.run(handle_status(message, orchestrator))

    message.answer.assert_awaited_once()
    response.json.assert_not_called()


def test_handle_continue_without_argument_shows_usage(message, orchestrator, poller):
    message.text = "/continue"
    orchestrator.activate = AsyncMock()

    asyncio.run(handle_continue(message, orchestrator, poller))

    orchestrator.activate.assert_not_awaited()
    message.answer.assert_awaited_once_with("Usage: /continue <project_id>")


def test_handle_continue_reports_unknown_project_on_404(message, orchestrator, poller):
    message.text = "/continue nope"
    orchestrator.activate = AsyncMock(return_value=_status_response(status_code=404))

    asyncio.run(handle_continue(message, orchestrator, poller))

    orchestrator.activate.assert_awaited_once_with("nope")
    message.answer.assert_awaited_once_with("Unknown project 'nope'. Try /projects.")
    poller.start.assert_not_called()


def test_handle_continue_reactivates_and_shows_pending_approval(message, orchestrator, poller):
    """The main use case: no message with buttons is available anymore (e.g.
    after a Docker stack restart), so /continue <id> must both reactivate
    the project (so a later free-text reply routes to it) and immediately
    show whatever it's currently waiting on."""
    message.text = "/continue proj-1"
    orchestrator.activate = AsyncMock(
        return_value=_status_response(
            status_code=200,
            project_id="proj-1",
            phase="awaiting_plan_approval",
            pending_approval={"thread_id": "proj-1", "summary": "Ready to build?"},
        )
    )

    asyncio.run(handle_continue(message, orchestrator, poller))

    orchestrator.activate.assert_awaited_once_with("proj-1")
    args, kwargs = message.answer.call_args
    assert args[0] == "Ready to build?"
    assert "reply_markup" in kwargs


def test_handle_continue_starts_progress_polling_for_the_resumed_project(
    message, orchestrator, poller
):
    """activate() can now resume a stuck project's execution from checkpoint
    and run the LangGraph synchronously, just like /webhook and /resume do --
    it needs the same live progress relay those already start, or the
    operator gets no updates while a long resumed run is in flight."""
    message.text = "/continue proj-1"
    orchestrator.activate = AsyncMock(return_value=_status_response(project_id="proj-1"))

    asyncio.run(handle_continue(message, orchestrator, poller))

    poller.start.assert_called_once_with("proj-1", 7)


def test_handle_continue_shows_task_progress_when_no_gate_pending(message, orchestrator, poller):
    message.text = "/continue proj-1"
    orchestrator.activate = AsyncMock(
        return_value=_status_response(
            status_code=200,
            project_id="proj-1",
            phase="implementing",
            pending_approval=None,
            task_progress={
                "done": 1,
                "total": 2,
                "tasks": [
                    {"description": "First task", "status": "done", "attempts": 1},
                    {"description": "Second task", "status": "in_progress", "attempts": 0},
                ],
            },
        )
    )

    asyncio.run(handle_continue(message, orchestrator, poller))

    message.answer.assert_awaited_once_with("1/2 tasks done\nIn progress: Second task")


def test_handle_continue_shows_a_hint_when_the_orchestrator_call_times_out(
    message, orchestrator, poller
):
    """activate() can now resume a stuck project's execution synchronously
    (OrchestratorService.activate), so it can be just as slow as /webhook or
    /resume against a local model -- /continue needs the same timeout
    fallback those already have."""
    message.text = "/continue proj-1"
    orchestrator.activate = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))

    asyncio.run(handle_continue(message, orchestrator, poller))

    message.answer.assert_awaited_once()


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


def test_handle_projects_shows_a_hint_on_server_error(message, orchestrator):
    """get_projects() can 5xx -- must not raise on response.json()."""
    message.text = "/projects"
    response = MagicMock()
    response.status_code = 500
    response.json.side_effect = ValueError("not JSON")
    orchestrator.get_projects = AsyncMock(return_value=response)

    asyncio.run(handle_projects(message, orchestrator))

    message.answer.assert_awaited_once()
    response.json.assert_not_called()


def test_handle_free_text_forwards_to_orchestrator(message, orchestrator, poller):
    message.text = "just chatting"

    asyncio.run(handle_free_text(message, orchestrator, poller))

    orchestrator.send_webhook.assert_awaited_once_with(42, "just chatting")
    poller.start.assert_called_once_with("proj-1", 7)


def test_handle_new_renders_an_approval_keyboard_when_a_gate_is_pending(
    message, orchestrator, poller
):
    response = MagicMock()
    response.status_code = 200
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
    response.status_code = 200
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


def test_handle_approval_callback_resumes_with_parsed_decision_and_thread_id(orchestrator, poller):
    callback = _callback("approve:thread-123")

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    orchestrator.resume.assert_awaited_once_with("thread-123", "approve")
    callback.answer.assert_awaited_once_with("Approved.")
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


def test_handle_approval_callback_uses_real_past_tense_for_reject(orchestrator, poller):
    callback = _callback("reject:thread-9")

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    orchestrator.resume.assert_awaited_once_with("thread-9", "reject")
    callback.answer.assert_awaited_once_with("Rejected.")


def test_handle_approval_callback_pushes_the_next_gates_keyboard(orchestrator, poller):
    """Approving the plan advances the graph straight to the merge gate; its
    approval keyboard must be pushed immediately, not stall until the user
    happens to send another message."""
    merge_gate = MagicMock()
    merge_gate.status_code = 200
    merge_gate.json.return_value = {
        "project_id": "proj-1",
        "phase": "awaiting_merge_approval",
        "pending_approval": {"thread_id": "proj-1", "summary": "Tests passed -- merge to main?"},
    }
    orchestrator.resume = AsyncMock(return_value=merge_gate)
    callback = _callback("approve:proj-1")

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    args, kwargs = callback.message.answer.call_args
    assert args[0] == "Tests passed -- merge to main?"
    assert "reply_markup" in kwargs


def test_handle_approval_callback_reports_terminal_phase_after_reject(orchestrator, poller):
    blocked = MagicMock()
    blocked.status_code = 200
    blocked.json.return_value = {
        "project_id": "proj-1",
        "phase": "blocked",
        "pending_approval": None,
    }
    orchestrator.resume = AsyncMock(return_value=blocked)
    callback = _callback("reject:proj-1")

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    callback.message.answer.assert_awaited_once_with("Project proj-1 is blocked.")


def test_handle_approval_callback_acknowledges_and_clears_keyboard_before_the_slow_resume_call(
    orchestrator, poller
):
    """orchestrator.resume() runs a LangGraph turn synchronously and can take
    a minute+ against a local model (observed: 58-62s in production). Telegram
    invalidates a callback query's answer token long before that ("query is
    too old and response timeout expired"), so answering and clearing the
    keyboard must happen before the slow call, not after -- otherwise the
    handler crashes on callback.answer() and the keyboard never clears."""
    callback = _callback("approve:thread-123")

    async def _resume_after_ack_and_clear(*args, **kwargs):
        assert callback.answer.await_count == 1
        assert callback.message.edit_reply_markup.await_count == 1
        return _webhook_response()

    orchestrator.resume = AsyncMock(side_effect=_resume_after_ack_and_clear)

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    orchestrator.resume.assert_awaited_once_with("thread-123", "approve")


def test_handle_approval_callback_shows_a_hint_when_the_resume_call_times_out(orchestrator, poller):
    """resume() can now run node_coder's task loop through every remaining
    task in one call and outlast even the Gateway's generous httpx timeout
    (observed: 300s+ once real task execution started working). Unlike
    handle_new/handle_free_text/handle_continue, this handler had no
    httpx.TimeoutException guard at all, so it crashed uncaught -- the tap
    was acknowledged and the keyboard cleared (the earlier ordering fix), but
    the user never got any follow-up message."""
    callback = _callback("approve:thread-123")
    orchestrator.resume = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    callback.message.answer.assert_awaited_once()


def test_handle_approval_callback_restarts_progress_polling_for_the_resumed_project(
    orchestrator, poller
):
    """Every gate in this graph needs a human tap (the merge gate always
    does, per .claude/rules/git.md), and the poller started at /new has
    normally stopped by then. Without restarting it here nothing watches the
    execution the approval just unblocked, so the terminal done/blocked
    notice never arrives."""
    callback = _callback("approve:proj-1")
    callback.message.chat.id = 7

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    poller.start.assert_called_once_with("proj-1", 7)


def test_handle_approval_callback_does_not_start_polling_when_the_resume_call_crashed(
    orchestrator, poller
):
    """A 5xx resume response has no JSON body to read a project_id out of and
    nothing worth polling for anyway."""
    crashed = MagicMock()
    crashed.status_code = 500
    crashed.json.side_effect = ValueError("not JSON")
    orchestrator.resume = AsyncMock(return_value=crashed)
    callback = _callback("approve:proj-1")

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    poller.start.assert_not_called()


def test_handle_approval_callback_handles_missing_message_gracefully(orchestrator, poller):
    callback = MagicMock()
    callback.data = "reject:thread-9"
    callback.answer = AsyncMock()
    callback.message = None

    asyncio.run(handle_approval_callback(callback, orchestrator, poller))

    orchestrator.resume.assert_awaited_once_with("thread-9", "reject")


def test_handle_free_text_shows_a_hint_when_text_was_not_applied(message, orchestrator, poller):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "project_id": "proj-1",
        "phase": "awaiting_merge_approval",
        "pending_approval": {"thread_id": "proj-1", "summary": "Merge to main?"},
        "text_applied": False,
    }
    orchestrator.send_webhook = AsyncMock(return_value=response)

    asyncio.run(handle_free_text(message, orchestrator, poller))

    message.answer.assert_awaited_once_with(
        "Project proj-1 is at phase 'awaiting_merge_approval' and isn't waiting on typed "
        "replies right now -- use the buttons above, or /status."
    )


def test_handle_free_text_shows_a_hint_when_the_orchestrator_returns_a_server_error(
    message, orchestrator, poller
):
    """A 5xx response means the graph itself crashed on an uncaught exception
    (e.g. a provider error mid-tool-call) -- FastAPI's default error body for
    that isn't JSON, so calling response.json() on it raised an unhandled
    JSONDecodeError in production instead of telling the user anything."""
    response = MagicMock()
    response.status_code = 500
    response.json.side_effect = ValueError("not JSON")
    orchestrator.send_webhook = AsyncMock(return_value=response)

    asyncio.run(handle_free_text(message, orchestrator, poller))

    message.answer.assert_awaited_once()
    response.json.assert_not_called()


def test_handle_reply_callback_activates_the_project_and_prompts_for_a_reply(orchestrator):
    from aiogram.types import ForceReply

    orchestrator.activate = AsyncMock(return_value=_status_response(project_id="proj-1"))
    callback = _callback("reply:proj-1")

    asyncio.run(handle_reply_callback(callback, orchestrator))

    orchestrator.activate.assert_awaited_once_with("proj-1")
    callback.answer.assert_awaited_once()
    callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)
    args, kwargs = callback.message.answer.call_args
    assert isinstance(kwargs.get("reply_markup"), ForceReply)


def test_handle_reply_callback_acknowledges_and_clears_keyboard_before_the_slow_activate_call(
    orchestrator,
):
    """activate() can now resume a stuck project's execution synchronously
    (OrchestratorService.activate), the same slow-call class that made
    handle_approval_callback answer the tap before calling resume(). An old
    Reply button tapped on a project that's since moved past its interview
    must not crash on callback.answer() before the keyboard is cleared."""
    callback = _callback("reply:proj-1")

    async def _activate_after_ack_and_clear(*args, **kwargs):
        assert callback.answer.await_count == 1
        assert callback.message.edit_reply_markup.await_count == 1
        return _status_response(project_id="proj-1")

    orchestrator.activate = AsyncMock(side_effect=_activate_after_ack_and_clear)

    asyncio.run(handle_reply_callback(callback, orchestrator))

    orchestrator.activate.assert_awaited_once_with("proj-1")


def test_handle_reply_callback_reports_unknown_project_on_404(orchestrator):
    """activate() can 404 the same way it does in handle_continue -- a stale
    Reply button tapped on a project that's been deleted/renamed must not
    silently show the ForceReply prompt with nothing valid to receive it."""
    orchestrator.activate = AsyncMock(return_value=_status_response(status_code=404))
    callback = _callback("reply:nope")

    asyncio.run(handle_reply_callback(callback, orchestrator))

    orchestrator.activate.assert_awaited_once_with("nope")
    callback.message.answer.assert_awaited_once_with("Unknown project 'nope'. Try /projects.")


def test_handle_reply_callback_shows_a_hint_on_server_error(orchestrator):
    """activate() can 5xx if the Orchestrator crashes resuming the project --
    this must not fall through to the ForceReply prompt, which would tell
    the operator the project is healthy and waiting on their input when it
    may not be in the state they think it's in."""
    from aiogram.types import ForceReply

    response = MagicMock()
    response.status_code = 500
    response.json.side_effect = ValueError("not JSON")
    orchestrator.activate = AsyncMock(return_value=response)
    callback = _callback("reply:proj-1")

    asyncio.run(handle_reply_callback(callback, orchestrator))

    orchestrator.activate.assert_awaited_once_with("proj-1")
    callback.message.answer.assert_awaited_once()
    args, kwargs = callback.message.answer.call_args
    assert not isinstance(kwargs.get("reply_markup"), ForceReply)


def test_handle_reply_callback_shows_a_hint_when_the_activate_call_times_out(orchestrator):
    """activate() can now resume a stuck project's execution and outlast the
    Gateway's httpx timeout the same way resume() can -- must not crash
    uncaught, must tell the user something."""
    callback = _callback("reply:proj-1")
    orchestrator.activate = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))

    asyncio.run(handle_reply_callback(callback, orchestrator))

    callback.message.answer.assert_awaited_once()


def test_handle_free_text_shows_a_hint_when_the_orchestrator_call_times_out(
    message, orchestrator, poller
):
    """A slow local-model turn can outlast the Gateway's httpx read timeout
    even though the Orchestrator keeps running server-side (main.py's
    DEFAULT_HTTP_TIMEOUT). The user must get some reply, not silence."""
    orchestrator.send_webhook = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))

    asyncio.run(handle_free_text(message, orchestrator, poller))

    message.answer.assert_awaited_once()
    poller.start.assert_not_called()


def test_handle_new_shows_a_hint_when_the_orchestrator_call_times_out(
    message, orchestrator, poller
):
    orchestrator.send_webhook = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))

    asyncio.run(handle_new(message, orchestrator, poller))

    message.answer.assert_awaited_once()
    poller.start.assert_not_called()


def test_handle_reply_callback_handles_missing_message_gracefully(orchestrator):
    orchestrator.activate = AsyncMock(return_value=_status_response(project_id="proj-1"))
    callback = MagicMock()
    callback.data = "reply:proj-1"
    callback.answer = AsyncMock()
    callback.message = None

    asyncio.run(handle_reply_callback(callback, orchestrator))

    orchestrator.activate.assert_awaited_once_with("proj-1")
