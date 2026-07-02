"""Authorization middleware: the single-operator allow-list (ARCHITECTURE.md §4.1)."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)


class AllowedUserMiddleware(BaseMiddleware):
    """Drops any update from a user other than the configured allowed_user_id.

    Registered on the Dispatcher globally so it covers every update type
    (messages, callback queries, ...) through aiogram's `event_from_user`
    context value. A dropped update produces no reply at all -- it's logged,
    not answered.
    """

    def __init__(self, allowed_user_id: int) -> None:
        """Initializes the middleware.

        Args:
            allowed_user_id: The single Telegram user ID allowed to interact
                with the bot, set during first-run setup.
        """
        self._allowed_user_id = allowed_user_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Calls handler only if the update's user matches allowed_user_id."""
        user = data.get("event_from_user")
        if user is None or user.id != self._allowed_user_id:
            logger.warning(
                "dropped update from unauthorized user_id=%s",
                user.id if user is not None else None,
            )
            return None
        return await handler(event, data)
