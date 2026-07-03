"""aiogram handlers: commands, free-text forwarding, and approval callbacks.

Handlers declare `orchestrator: OrchestratorClient` as a parameter; aiogram
injects it from the Dispatcher's workflow data (set once in main.py), so
these stay plain, directly callable functions -- no closures to unwrap in
tests.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from services.telegram_gateway.orchestrator_client import OrchestratorClient
from services.telegram_gateway.progress_poller import ProgressPoller
from services.telegram_gateway.rendering import (
    CALLBACK_APPROVE,
    CALLBACK_EDIT,
    CALLBACK_REJECT,
    render_approval,
)

logger = logging.getLogger(__name__)

router = Router(name="foundry_gateway")


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
    """Renders a webhook response: an Approve/Reject/Edit keyboard if the
    project is now paused at a gate, a plain acknowledgment otherwise.

    Args:
        message: The incoming message to reply to.
        response: The httpx.Response returned by OrchestratorClient.send_webhook.
    """
    pending_approval = response.json().get("pending_approval")
    if pending_approval:
        text, keyboard = render_approval(pending_approval)
        await message.answer(text, reply_markup=keyboard)
    else:
        await message.answer("Got it -- working on that.")


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    """Greets the user. Sends nothing to the Orchestrator."""
    await message.answer("Foundry is online. Send /new to start a project.")


@router.message(Command("new", "status", "projects"))
async def handle_command(
    message: Message, orchestrator: OrchestratorClient, poller: ProgressPoller
) -> None:
    """Forwards a /new, /status, or /projects command to the Orchestrator."""
    response = await orchestrator.send_webhook(message.from_user.id, message.text)
    _start_poller_from_response(poller, response, message.chat.id)
    await _relay_webhook_result(message, response)


@router.message(F.text)
async def handle_free_text(
    message: Message, orchestrator: OrchestratorClient, poller: ProgressPoller
) -> None:
    """Forwards any other free-text message to the Orchestrator."""
    response = await orchestrator.send_webhook(message.from_user.id, message.text)
    _start_poller_from_response(poller, response, message.chat.id)
    await _relay_webhook_result(message, response)


@router.callback_query(F.data.startswith((CALLBACK_APPROVE, CALLBACK_REJECT, CALLBACK_EDIT)))
async def handle_approval_callback(
    callback: CallbackQuery, orchestrator: OrchestratorClient
) -> None:
    """Resolves an Approve/Reject/Edit button press via POST /resume/{thread_id}."""
    decision, thread_id = callback.data.split(":", 1)
    await orchestrator.resume(thread_id, decision)
    await callback.answer(f"{decision.capitalize()}d.")
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
