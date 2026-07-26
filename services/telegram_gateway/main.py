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
from services.telegram_gateway.progress_poller import ProgressPoller

DEFAULT_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0)
"""Generous read timeout: POST /webhook runs the LangGraph synchronously up
to its next interrupt, which can chain several sequential LLM calls (e.g.
interview complete -> plan drafted -> critique) before responding. Against a
local model this routinely exceeds httpx's 5s default, which aborted the
request client-side while the Orchestrator kept running -- the user's
message appeared to vanish even though the project advanced."""


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

    async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT) as client:
        orchestrator = OrchestratorClient(config.orchestrator_url, client)
        poller = ProgressPoller(
            orchestrator,
            bot,
            poll_interval_seconds=config.poll_interval_seconds,
            max_idle_polls=config.max_idle_polls,
        )
        await dispatcher.start_polling(bot, orchestrator=orchestrator, poller=poller)


def main() -> None:
    """Reads config from the environment and starts the bot."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(GatewayConfig.from_env()))


if __name__ == "__main__":
    main()
