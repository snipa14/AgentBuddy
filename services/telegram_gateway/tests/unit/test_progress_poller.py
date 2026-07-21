"""Unit tests for ProgressPoller (ADR-027, TODO.md Phase 16).

Drives `_poll_once` directly rather than the real `_poll_loop`/`asyncio.sleep`
cadence, so these tests run instantly and deterministically -- exactly the
same "call the plain function/method directly" style already used for the
Gateway's message/callback handlers in test_bot_handlers.py.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.telegram_gateway.progress_poller import TELEGRAM_MESSAGE_LIMIT, ProgressPoller


def _stream_response(chunks: list[dict]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"chunks": chunks}
    return response


def _status_response(phase: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"phase": phase}
    return response


def _fake_orchestrator(stream_results: list[MagicMock], status_results: list[MagicMock]):
    orchestrator = MagicMock()
    orchestrator.stream = AsyncMock(side_effect=stream_results)
    orchestrator.get_status = AsyncMock(side_effect=status_results)
    return orchestrator


def _fake_bot():
    bot = MagicMock()
    sent = MagicMock()
    sent.message_id = 99
    bot.send_message = AsyncMock(return_value=sent)
    bot.edit_message_text = AsyncMock()
    return bot


def test_poll_once_sends_a_message_on_first_chunks():
    orchestrator = _fake_orchestrator(
        [_stream_response([{"seq": 0, "text": "hello "}])],
        [_status_response("implementing")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is True
    bot.send_message.assert_awaited_once_with(7, "hello ")
    bot.edit_message_text.assert_not_awaited()


def test_poll_once_edits_the_same_message_on_subsequent_chunks():
    orchestrator = _fake_orchestrator(
        [
            _stream_response([{"seq": 0, "text": "hello "}]),
            _stream_response([{"seq": 1, "text": "world"}]),
        ],
        [_status_response("implementing"), _status_response("implementing")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    asyncio.run(poller._poll_once("proj-1", chat_id=7))
    asyncio.run(poller._poll_once("proj-1", chat_id=7))

    bot.send_message.assert_awaited_once_with(7, "hello ")
    bot.edit_message_text.assert_awaited_once_with(
        "hello world", chat_id=7, message_id=99
    )


def test_poll_once_never_requests_an_already_seen_chunk_again():
    orchestrator = _fake_orchestrator(
        [_stream_response([{"seq": 0, "text": "a"}, {"seq": 1, "text": "b"}])],
        [_status_response("implementing")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert poller._since_seq["proj-1"] == 1
    orchestrator.stream.assert_awaited_once_with("proj-1", -1)


def test_poll_once_stops_once_the_project_reaches_a_terminal_phase():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("done")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    assert "proj-1" not in poller._since_seq


def test_poll_once_stops_after_idle_timeout():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("implementing")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=1)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False


def test_poll_once_resilient_survives_a_transient_orchestrator_error():
    """A momentarily-unreachable Orchestrator must count as an idle poll and
    keep the loop alive, not kill the asyncio task silently."""
    orchestrator = MagicMock()
    orchestrator.stream = AsyncMock(side_effect=RuntimeError("connection refused"))
    poller = ProgressPoller(orchestrator, _fake_bot())
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once_resilient("proj-1", chat_id=7))

    assert should_continue is True
    assert poller._idle_polls["proj-1"] == 1


def test_poll_once_resilient_gives_up_after_idle_budget_of_failures():
    orchestrator = MagicMock()
    orchestrator.stream = AsyncMock(side_effect=RuntimeError("connection refused"))
    poller = ProgressPoller(orchestrator, _fake_bot(), max_idle_polls=2)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 1

    should_continue = asyncio.run(poller._poll_once_resilient("proj-1", chat_id=7))

    assert should_continue is False


def test_poll_once_truncates_the_telegram_message_but_keeps_the_full_text_internally():
    first_chunk = "a" * (TELEGRAM_MESSAGE_LIMIT - 10)
    second_chunk = "b" * 100
    orchestrator = _fake_orchestrator(
        [
            _stream_response([{"seq": 0, "text": first_chunk}]),
            _stream_response([{"seq": 1, "text": second_chunk}]),
        ],
        [_status_response("implementing"), _status_response("implementing")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    asyncio.run(poller._poll_once("proj-1", chat_id=7))
    asyncio.run(poller._poll_once("proj-1", chat_id=7))

    sent_text = bot.edit_message_text.call_args.args[0]
    assert len(sent_text) <= TELEGRAM_MESSAGE_LIMIT
    assert sent_text.endswith(second_chunk)
    assert poller._messages["proj-1"][1] == first_chunk + second_chunk


def test_start_does_not_spawn_a_second_task_for_an_already_running_project():
    orchestrator = _fake_orchestrator([], [])
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)

    async def _never_finishes(*args, **kwargs):
        await asyncio.sleep(3600)

    async def _run():
        poller._poll_loop = _never_finishes
        poller.start("proj-1", chat_id=7)
        first_task = poller._tasks["proj-1"]
        poller.start("proj-1", chat_id=7)
        assert poller._tasks["proj-1"] is first_task
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
