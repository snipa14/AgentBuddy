"""aiogram handlers: commands, free-text forwarding, and approval callbacks.

Handlers declare `orchestrator: OrchestratorClient` as a parameter; aiogram
injects it from the Dispatcher's workflow data (set once in main.py), so
these stay plain, directly callable functions -- no closures to unwrap in
tests.

Only `/new` and free text reach `POST /webhook` (both can legitimately start
or address a project's intake conversation). `/status` and `/projects` are
read-only lookups against `GET /status/{id}` / `GET /projects` -- forwarding
them to the webhook, as this module originally did, silently started a new
junk project (with the literal command text as its intake message, and a
real Planner LLM call) on every status check.
"""

import logging

import httpx
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ForceReply, Message

from services.telegram_gateway.orchestrator_client import OrchestratorClient
from services.telegram_gateway.progress_poller import ProgressPoller
from services.telegram_gateway.rendering import (
    CALLBACK_APPROVE,
    CALLBACK_EDIT,
    CALLBACK_REJECT,
    CALLBACK_REPLY,
    render_approval,
    render_project_list,
    render_project_status,
    render_task_progress,
)

logger = logging.getLogger(__name__)

router = Router(name="foundry_gateway")

_DECISION_ACKNOWLEDGMENTS = {
    CALLBACK_APPROVE: "Approved.",
    CALLBACK_REJECT: "Rejected.",
    CALLBACK_EDIT: "Edit requested.",
}


def _start_poller_from_response(poller: ProgressPoller, response, chat_id: int) -> None:
    """Starts live progress polling for the project a webhook response names.

    Args:
        poller: The Gateway's shared ProgressPoller.
        response: The httpx.Response returned by OrchestratorClient.send_webhook.
        chat_id: The Telegram chat to relay this project's progress into.
    """
    project_id = response.json().get("project_id")
    if project_id:
        poller.start(project_id, chat_id)


async def _relay_webhook_result(message: Message, response) -> None:
    """Renders a webhook response: an explicit hint if the text wasn't
    applied to any project, an Approve/Reject/Edit or Reply keyboard if the
    project is now paused at a gate, a task-progress summary if tasks
    exist, a plain acknowledgment otherwise.

    Args:
        message: The incoming message to reply to.
        response: The httpx.Response returned by OrchestratorClient.send_webhook.
    """
    body = response.json()
    if body.get("text_applied") is False:
        await message.answer(
            f"Project {body['project_id']} is at phase '{body['phase']}' and isn't "
            "waiting on typed replies right now -- use the buttons above, or /status."
        )
        return
    pending_approval = body.get("pending_approval")
    if pending_approval:
        text, keyboard = render_approval(pending_approval)
        await message.answer(text, reply_markup=keyboard)
        return
    task_progress = body.get("task_progress")
    if task_progress:
        await message.answer(render_task_progress(task_progress))
        return
    phase = body.get("phase")
    if phase in ("done", "blocked"):
        await message.answer(f"Project {body.get('project_id')} is {phase}.")
        return
    await message.answer("Got it -- working on that.")


async def _answer_timeout_hint(message: Message) -> None:
    """Tells the user their message was received but the Orchestrator hasn't
    replied within the Gateway's client timeout. The graph run keeps going
    server-side regardless -- POST /webhook runs it synchronously to the next
    interrupt and a slow local-model chain (e.g. interview -> plan ->
    critique) can outlast the client's read timeout -- so this isn't a
    failure, just a slow turn.
    """
    await message.answer(
        "Still working on that -- a local-model turn can take a while, "
        "especially mid-interview. Check /status in a bit."
    )


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    """Greets the user. Sends nothing to the Orchestrator."""
    await message.answer("Foundry is online. Send /new to start a project.")


@router.message(Command("new"))
async def handle_new(
    message: Message, orchestrator: OrchestratorClient, poller: ProgressPoller
) -> None:
    """Forwards a /new command to the Orchestrator to start a project."""
    try:
        response = await orchestrator.send_webhook(
            message.from_user.id, message.text, force_new=True
        )
    except httpx.TimeoutException:
        await _answer_timeout_hint(message)
        return
    _start_poller_from_response(poller, response, message.chat.id)
    await _relay_webhook_result(message, response)


@router.message(Command("status"))
async def handle_status(message: Message, orchestrator: OrchestratorClient) -> None:
    """Reports one project's phase via GET /status/{project_id}.

    `/status <project_id>` looks that project up; a bare `/status` lists the
    known project ids to pick from instead of guessing.
    """
    parts = (message.text or "").split(maxsplit=1)
    project_id = parts[1].strip() if len(parts) > 1 else ""
    if not project_id:
        projects_response = await orchestrator.get_projects()
        project_ids = projects_response.json().get("project_ids", [])
        await message.answer(
            "Usage: /status <project_id>\n" + render_project_list(project_ids)
        )
        return

    response = await orchestrator.get_status(project_id)
    if response.status_code == 404:
        await message.answer(f"Unknown project '{project_id}'. Try /projects.")
        return
    await message.answer(render_project_status(response.json()))


@router.message(Command("projects"))
async def handle_projects(message: Message, orchestrator: OrchestratorClient) -> None:
    """Lists every known project id via GET /projects."""
    response = await orchestrator.get_projects()
    await message.answer(render_project_list(response.json().get("project_ids", [])))


@router.message(F.text)
async def handle_free_text(
    message: Message, orchestrator: OrchestratorClient, poller: ProgressPoller
) -> None:
    """Forwards any other free-text message to the Orchestrator."""
    try:
        response = await orchestrator.send_webhook(message.from_user.id, message.text)
    except httpx.TimeoutException:
        await _answer_timeout_hint(message)
        return
    _start_poller_from_response(poller, response, message.chat.id)
    await _relay_webhook_result(message, response)


@router.callback_query(F.data.startswith((CALLBACK_APPROVE, CALLBACK_REJECT, CALLBACK_EDIT)))
async def handle_approval_callback(
    callback: CallbackQuery, orchestrator: OrchestratorClient
) -> None:
    """Resolves an Approve/Reject/Edit button press via POST /resume/{thread_id}.

    The resume response is relayed back into the chat: a project that
    advances straight to its next gate (plan approval -> merge approval)
    gets that gate's keyboard pushed immediately, instead of stalling
    silently until the user happens to send another message.
    """
    decision, thread_id = callback.data.split(":", 1)
    response = await orchestrator.resume(thread_id, decision)
    await callback.answer(_DECISION_ACKNOWLEDGMENTS.get(decision, "Done."))
    if callback.message is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await _relay_webhook_result(callback.message, response)


@router.callback_query(F.data.startswith(CALLBACK_REPLY))
async def handle_reply_callback(callback: CallbackQuery, orchestrator: OrchestratorClient) -> None:
    """Activates the tapped project and opens a force-reply text box.

    The Reply button on an interview-question message is the disambiguator
    for when more than one project is paused at once (the common
    single-project case never needs it -- see docs/superpowers/specs/
    2026-07-25-telegram-conversational-loop-design.md).
    """
    _, project_id = callback.data.split(":", 1)
    await orchestrator.activate(project_id)
    await callback.answer()
    if callback.message is None:
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Type your answer:", reply_markup=ForceReply(selective=True)
    )
