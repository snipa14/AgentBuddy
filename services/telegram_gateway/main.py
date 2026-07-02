"""Entrypoint: runs the Telegram Gateway via long polling.

Long polling, not webhooks, per ARCHITECTURE.md §4.1 -- simpler for an
on-demand host without a stable public IP (ADR-002, ADR-010).
"""

import asyncio
import logging

import httpx
from aiogram import Bot, Dispatcher

from services.telegram_gateway.auth import AllowedUserMiddleware
from services.telegram_gateway.bot import router
from services.telegram_gateway.config import GatewayConfig
from services.telegram_gateway.orchestrator_client import OrchestratorClient


async def run(config: GatewayConfig) -> None:
    """Builds and runs the bot via long polling until interrupted.

    Args:
        config: The Gateway's runtime configuration.
    """
    bot = Bot(token=config.bot_token)
    dispatcher = Dispatcher()
    auth_middleware = AllowedUserMiddleware(config.allowed_user_id)
    dispatcher.message.outer_middleware(auth_middleware)
    dispatcher.callback_query.outer_middleware(auth_middleware)
    dispatcher.include_router(router)

    async with httpx.AsyncClient() as client:
        orchestrator = OrchestratorClient(config.orchestrator_url, client)
        await dispatcher.start_polling(bot, orchestrator=orchestrator)


def main() -> None:
    """Reads config from the environment and starts the bot."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(GatewayConfig.from_env()))


if __name__ == "__main__":
    main()
