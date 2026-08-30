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

logger = logging.getLogger(__name__)

_TERMINAL_PHASES = frozenset({"done", "blocked"})

DEFAULT_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=1800.0, write=30.0, pool=30.0)
"""Generous read timeout: POST /webhook, /resume, and /activate all run the
LangGraph synchronously up to its next interrupt, which can chain several
sequential LLM calls (e.g. interview complete -> plan drafted -> critique)
before responding. Against a local model this routinely exceeds httpx's 5s
default, which aborted the request client-side while the Orchestrator kept
running -- the user's message appeared to vanish even though the project
advanced. 300s (the original fix) was itself too short once real task
execution started working: /resume can run node_coder's task loop through
every remaining task in one call, observed taking over 5 minutes for a
single multi-task run against a local model. 1800s (30 minutes) is a
pragmatic ceiling, not a structural fix -- a sufficiently large plan can
still legitimately outlast it, at which point the real fix is making these
endpoints non-blocking (return immediately, let the poller carry progress)
rather than raising this number again."""


async def rearm_pollers(
    orchestrator: OrchestratorClient, poller: ProgressPoller, bot: Bot, chat_id: int
) -> None:
    """Re-arms progress polling for every non-terminal project after a restart.

    A Gateway restart (deploy, crash, `docker compose restart`) drops every
    in-flight `ProgressPoller` task silently -- they're plain in-process
    `asyncio` state, never persisted -- so a project that was mid-task when
    the previous process died just stops being watched, with no notice to
    the operator that anything was interrupted. This re-reads every known
    project from the Orchestrator (whose own state survives a restart via
    the LangGraph checkpointer, ARCHITECTURE.md §5) and starts a fresh
    poller for each one still non-terminal, then tells the operator what it
    resumed -- so the gap doesn't have to be noticed and worked around with
    a manual /continue.

    A failure reading the project list or one project's status is logged
    and otherwise swallowed: this runs once at startup, before the bot
    starts polling Telegram at all, so it must never prevent the Gateway
    from coming up.

    Args:
        orchestrator: The Orchestrator client to read project state from.
        poller: The Gateway's shared ProgressPoller.
        bot: The aiogram Bot used to send the restart notice.
        chat_id: The Telegram chat to relay progress and the notice into.
    """
    try:
        response = await orchestrator.get_projects()
        projects = response.json().get("projects", [])
    except Exception:
        logger.warning("startup: failed to list projects for poller re-arm", exc_info=True)
        return

    resumed_labels = []
    for project in projects:
        project_id = project["id"]
        try:
            status_response = await orchestrator.get_status(project_id)
            phase = status_response.json().get("phase")
        except Exception:
            logger.warning(
                "startup: failed to read status for project %s", project_id, exc_info=True
            )
            continue
        if phase not in _TERMINAL_PHASES:
            poller.start(project_id, chat_id)
            resumed_labels.append(project.get("name") or project_id)

    if not resumed_labels:
        return
    try:
        await bot.send_message(
            chat_id,
            "🔄 Foundry restarted -- still tracking "
            f"{len(resumed_labels)} project(s): {', '.join(resumed_labels)}",
        )
    except Exception:
        logger.warning("startup: failed to send restart notice", exc_info=True)


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
        await rearm_pollers(orchestrator, poller, bot, config.allowed_user_id)
        await dispatcher.start_polling(bot, orchestrator=orchestrator, poller=poller)


def main() -> None:
    """Reads config from the environment and starts the bot."""
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(GatewayConfig.from_env()))


if __name__ == "__main__":
    main()
