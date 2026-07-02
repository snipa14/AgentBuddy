"""Unit tests for AllowedUserMiddleware."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.telegram_gateway.auth import AllowedUserMiddleware


def test_allowed_user_middleware_calls_handler_for_the_allowed_user():
    middleware = AllowedUserMiddleware(allowed_user_id=42)
    handler = AsyncMock(return_value="handled")
    user = MagicMock(id=42)
    event = MagicMock()
    data = {"event_from_user": user}

    result = asyncio.run(middleware(handler, event, data))

    assert result == "handled"
    handler.assert_awaited_once_with(event, data)


def test_allowed_user_middleware_drops_update_from_another_user():
    middleware = AllowedUserMiddleware(allowed_user_id=42)
    handler = AsyncMock()
    user = MagicMock(id=999)
    event = MagicMock()

    result = asyncio.run(middleware(handler, event, {"event_from_user": user}))

    assert result is None
    handler.assert_not_awaited()


def test_allowed_user_middleware_drops_update_with_no_user_present():
    middleware = AllowedUserMiddleware(allowed_user_id=42)
    handler = AsyncMock()
    event = MagicMock()

    result = asyncio.run(middleware(handler, event, {}))

    assert result is None
    handler.assert_not_awaited()


def test_allowed_user_middleware_logs_dropped_update(caplog):
    middleware = AllowedUserMiddleware(allowed_user_id=42)
    handler = AsyncMock()
    user = MagicMock(id=999)
    event = MagicMock()

    with caplog.at_level("WARNING"):
        asyncio.run(middleware(handler, event, {"event_from_user": user}))

    assert "999" in caplog.text
