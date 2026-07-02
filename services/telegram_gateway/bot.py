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
from services.telegram_gateway.rendering import CALLBACK_APPROVE, CALLBACK_EDIT, CALLBACK_REJECT

logger = logging.getLogger(__name__)

router = Router(name="foundry_gateway")


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    """Greets the user. Sends nothing to the Orchestrator."""
    await message.answer("Foundry is online. Send /new to start a project.")


@router.message(Command("new", "status", "projects"))
async def handle_command(message: Message, orchestrator: OrchestratorClient) -> None:
    """Forwards a /new, /status, or /projects command to the Orchestrator."""
    await orchestrator.send_webhook(message.from_user.id, message.text)
    await message.answer("Got it -- working on that.")


@router.message(F.text)
async def handle_free_text(message: Message, orchestrator: OrchestratorClient) -> None:
    """Forwards any other free-text message to the Orchestrator."""
    await orchestrator.send_webhook(message.from_user.id, message.text)


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
