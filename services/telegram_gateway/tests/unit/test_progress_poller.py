"""Unit tests for ProgressPoller (ADR-027, TODO.md Phase 16).

Drives `_poll_once` directly rather than the real `_poll_loop`/`asyncio.sleep`
cadence, so these tests run instantly and deterministically -- exactly the
same "call the plain function/method directly" style already used for the
Gateway's message/callback handlers in test_bot_handlers.py.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.telegram_gateway.progress_poller import (
    _GIVE_UP_MESSAGE,
    TELEGRAM_MESSAGE_LIMIT,
    ProgressPoller,
)


def _stream_response(chunks: list[dict]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"chunks": chunks}
    return response


def _status_response(phase: str, **body) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"phase": phase, **body}
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
    bot.edit_message_text.assert_awaited_once_with("hello \nworld", chat_id=7, message_id=99)


def test_poll_once_puts_each_event_from_one_fetch_on_its_own_line():
    """Chunks are discrete progress events, not deltas of one streamed
    sentence -- several arriving in a single /stream fetch must render one
    per line, not run together into an unreadable single line."""
    orchestrator = _fake_orchestrator(
        [
            _stream_response(
                [
                    {"seq": 0, "text": "🔧 write_file src/a.py"},
                    {"seq": 1, "text": "✅ write_file src/a.py"},
                ]
            )
        ],
        [_status_response("implementing")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    asyncio.run(poller._poll_once("proj-1", chat_id=7))

    bot.send_message.assert_awaited_once_with(7, "🔧 write_file src/a.py\n✅ write_file src/a.py")


def test_poll_once_prefixes_the_first_message_with_the_project_name():
    orchestrator = _fake_orchestrator(
        [_stream_response([{"seq": 0, "text": "hello "}])],
        [_status_response("implementing", name="Build Me A Cli")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    asyncio.run(poller._poll_once("proj-1", chat_id=7))

    bot.send_message.assert_awaited_once_with(7, "📁 Build Me A Cli\nhello ")


def test_poll_once_does_not_re_send_the_name_header_on_a_later_edit():
    orchestrator = _fake_orchestrator(
        [
            _stream_response([{"seq": 0, "text": "hello "}]),
            _stream_response([{"seq": 1, "text": "world"}]),
        ],
        [
            _status_response("implementing", name="Build Me A Cli"),
            _status_response("implementing", name="Build Me A Cli"),
        ],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    asyncio.run(poller._poll_once("proj-1", chat_id=7))
    asyncio.run(poller._poll_once("proj-1", chat_id=7))

    bot.edit_message_text.assert_awaited_once_with(
        "📁 Build Me A Cli\nhello \nworld", chat_id=7, message_id=99
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


def test_poll_once_stops_and_sends_a_done_message_once_the_project_reaches_done():
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
    bot.send_message.assert_awaited_once_with(7, "✅ project done")


def test_poll_once_stops_and_sends_a_blocked_message_once_the_project_reaches_blocked():
    """A project blocked by something other than a task -- a rejected cost or
    merge gate -- has no blocked task to name, so no reason clause."""
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("blocked")],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    bot.send_message.assert_awaited_once_with(7, "🚫 project blocked")


def test_poll_once_names_the_blocked_task_as_the_reason_in_the_blocked_message():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [
            _status_response(
                "blocked",
                task_progress={
                    "done": 1,
                    "total": 2,
                    "tasks": [
                        {"description": "Add the parser", "status": "done", "attempts": 1},
                        {"description": "Wire up the CLI", "status": "blocked", "attempts": 3},
                    ],
                },
            )
        ],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    bot.send_message.assert_awaited_once_with(7, "🚫 project blocked: Wire up the CLI")


def test_poll_once_falls_back_to_a_bare_blocked_message_when_no_task_is_blocked():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [
            _status_response(
                "blocked",
                task_progress={
                    "done": 1,
                    "total": 1,
                    "tasks": [
                        {"description": "Add the parser", "status": "done", "attempts": 1},
                    ],
                },
            )
        ],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 0

    asyncio.run(poller._poll_once("proj-1", chat_id=7))

    bot.send_message.assert_awaited_once_with(7, "🚫 project blocked")


def test_poll_once_stops_polling_even_when_the_terminal_message_fails_to_send():
    """A failed Telegram send must not leave the poll loop running for a
    project that's genuinely finished -- the send is best-effort, the stop
    is not."""
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("done")],
    )
    bot = _fake_bot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("telegram rejected it"))
    poller = ProgressPoller(orchestrator, bot)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    assert "proj-1" not in poller._since_seq


def test_poll_once_resets_the_idle_budget_while_a_project_waits_at_an_approval_gate():
    """A project parked at a human approval gate produces no progress events
    by design (no gate calls add_progress), and every gate needs a human tap.
    Spending the idle budget there wrongly declares a healthy project lost
    after a few minutes of the operator not looking at their phone."""
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [
            _status_response(
                "awaiting_merge_approval",
                pending_approval={"thread_id": "proj-1", "summary": "Merge to main?"},
            )
        ],
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=5)
    poller._since_seq["proj-1"] = 5
    poller._idle_polls["proj-1"] = 4

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is True
    assert poller._idle_polls["proj-1"] == 0
    bot.send_message.assert_not_awaited()


def test_poll_once_idle_timeout_nudges_the_project_via_activate_and_reports_new_phase():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("implementing", name="Build Me A Cli")],
    )
    orchestrator.activate = AsyncMock(return_value=MagicMock(status_code=200))
    orchestrator.get_status = AsyncMock(
        side_effect=[
            _status_response("implementing", name="Build Me A Cli"),
            _status_response("awaiting_merge_approval", name="Build Me A Cli"),
        ]
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=1)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    orchestrator.activate.assert_awaited_once_with("proj-1")
    bot.send_message.assert_awaited_once_with(
        7,
        "📁 Build Me A Cli\n"
        "⚠️ Lost track for a bit -- nudged it back into motion, now at "
        "'awaiting_merge_approval'.",
    )


def test_poll_once_idle_timeout_falls_back_to_the_generic_message_when_activate_fails():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("implementing")],
    )
    orchestrator.activate = AsyncMock(side_effect=RuntimeError("orchestrator unreachable"))
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=1)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    bot.send_message.assert_awaited_once_with(7, _GIVE_UP_MESSAGE)


def test_poll_once_idle_timeout_reports_terminal_phase_when_activate_finishes_the_project():
    orchestrator = _fake_orchestrator(
        [_stream_response([])],
        [_status_response("implementing")],
    )
    orchestrator.activate = AsyncMock(return_value=MagicMock(status_code=200))
    orchestrator.get_status = AsyncMock(
        side_effect=[_status_response("implementing"), _status_response("done")]
    )
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=1)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 0

    should_continue = asyncio.run(poller._poll_once("proj-1", chat_id=7))

    assert should_continue is False
    bot.send_message.assert_awaited_once_with(7, "✅ project done")


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


def test_poll_once_resilient_sends_a_give_up_message_when_it_exhausts_the_idle_budget():
    orchestrator = MagicMock()
    orchestrator.stream = AsyncMock(side_effect=RuntimeError("connection refused"))
    orchestrator.activate = AsyncMock(side_effect=RuntimeError("connection refused"))
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=2)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 1

    should_continue = asyncio.run(poller._poll_once_resilient("proj-1", chat_id=7))

    assert should_continue is False
    bot.send_message.assert_awaited_once_with(7, _GIVE_UP_MESSAGE)


def test_poll_once_resilient_nudges_via_activate_when_it_exhausts_the_idle_budget():
    orchestrator = MagicMock()
    orchestrator.stream = AsyncMock(side_effect=RuntimeError("connection refused"))
    orchestrator.activate = AsyncMock(return_value=MagicMock(status_code=200))
    orchestrator.get_status = AsyncMock(return_value=_status_response("awaiting_merge_approval"))
    bot = _fake_bot()
    poller = ProgressPoller(orchestrator, bot, max_idle_polls=2)
    poller._since_seq["proj-1"] = -1
    poller._idle_polls["proj-1"] = 1

    should_continue = asyncio.run(poller._poll_once_resilient("proj-1", chat_id=7))

    assert should_continue is False
    orchestrator.activate.assert_awaited_once_with("proj-1")
    bot.send_message.assert_awaited_once_with(
        7,
        "⚠️ Lost track for a bit -- nudged it back into motion, now at 'awaiting_merge_approval'.",
    )


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
    assert poller._messages["proj-1"][1] == f"{first_chunk}\n{second_chunk}"


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
