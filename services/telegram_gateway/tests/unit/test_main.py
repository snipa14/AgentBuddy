"""Unit tests for `rearm_pollers()`, the Gateway's startup poller re-arm routine."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.telegram_gateway.main import rearm_pollers


def _projects_response(projects: list[dict]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"projects": projects}
    return response


def _status_response(phase: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"phase": phase}
    return response


def test_rearm_pollers_starts_polling_for_every_non_terminal_project():
    orchestrator = MagicMock()
    orchestrator.get_projects = AsyncMock(
        return_value=_projects_response(
            [{"id": "proj-1", "name": "First"}, {"id": "proj-2", "name": None}]
        )
    )
    orchestrator.get_status = AsyncMock(
        side_effect=[_status_response("implementing"), _status_response("awaiting_merge_approval")]
    )
    poller = MagicMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    asyncio.run(rearm_pollers(orchestrator, poller, bot, chat_id=7))

    poller.start.assert_any_call("proj-1", 7)
    poller.start.assert_any_call("proj-2", 7)
    assert poller.start.call_count == 2


def test_rearm_pollers_skips_terminal_projects():
    orchestrator = MagicMock()
    orchestrator.get_projects = AsyncMock(
        return_value=_projects_response([{"id": "proj-1", "name": None}])
    )
    orchestrator.get_status = AsyncMock(return_value=_status_response("done"))
    poller = MagicMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    asyncio.run(rearm_pollers(orchestrator, poller, bot, chat_id=7))

    poller.start.assert_not_called()
    bot.send_message.assert_not_awaited()


def test_rearm_pollers_sends_a_restart_notice_naming_resumed_projects():
    orchestrator = MagicMock()
    orchestrator.get_projects = AsyncMock(
        return_value=_projects_response([{"id": "proj-1", "name": "Build Me A Cli"}])
    )
    orchestrator.get_status = AsyncMock(return_value=_status_response("implementing"))
    poller = MagicMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    asyncio.run(rearm_pollers(orchestrator, poller, bot, chat_id=7))

    bot.send_message.assert_awaited_once()
    args = bot.send_message.call_args.args
    assert args[0] == 7
    assert "Build Me A Cli" in args[1]


def test_rearm_pollers_sends_no_notice_when_nothing_needs_resuming():
    orchestrator = MagicMock()
    orchestrator.get_projects = AsyncMock(return_value=_projects_response([]))
    poller = MagicMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    asyncio.run(rearm_pollers(orchestrator, poller, bot, chat_id=7))

    bot.send_message.assert_not_awaited()


def test_rearm_pollers_swallows_a_failure_listing_projects():
    orchestrator = MagicMock()
    orchestrator.get_projects = AsyncMock(side_effect=RuntimeError("connection refused"))
    poller = MagicMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    asyncio.run(rearm_pollers(orchestrator, poller, bot, chat_id=7))

    poller.start.assert_not_called()
    bot.send_message.assert_not_awaited()


def test_rearm_pollers_skips_but_continues_past_a_project_whose_status_fails():
    orchestrator = MagicMock()
    orchestrator.get_projects = AsyncMock(
        return_value=_projects_response(
            [{"id": "proj-broken", "name": None}, {"id": "proj-ok", "name": None}]
        )
    )
    orchestrator.get_status = AsyncMock(
        side_effect=[RuntimeError("boom"), _status_response("implementing")]
    )
    poller = MagicMock()
    bot = MagicMock()
    bot.send_message = AsyncMock()

    asyncio.run(rearm_pollers(orchestrator, poller, bot, chat_id=7))

    poller.start.assert_called_once_with("proj-ok", 7)
