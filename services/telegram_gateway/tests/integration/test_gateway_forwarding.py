"""Integration test: an authorized Telegram update reaches a stub Orchestrator.

Exercises the real Dispatcher + auth middleware + router + OrchestratorClient
chain. The Telegram Bot API side is faked (NullTelegramSession) so nothing
here ever calls the real Telegram API; the Orchestrator side is a real local
HTTP server (a stub -- Phase 5 builds the actual FastAPI Orchestrator app),
so the httpx POST that reaches it is a real network call, proving the whole
forwarding pipeline end to end rather than each piece in isolation.
"""

import asyncio
import json
import threading
import time
from collections.abc import AsyncGenerator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Update

from services.telegram_gateway.auth import AllowedUserMiddleware
from services.telegram_gateway.bot import router
from services.telegram_gateway.orchestrator_client import OrchestratorClient
from services.telegram_gateway.progress_poller import ProgressPoller

pytestmark = pytest.mark.integration

ALLOWED_USER_ID = 111
OTHER_USER_ID = 999


class NullTelegramSession(BaseSession):
    """Answers every Bot API call without touching the network."""

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):
        return True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        return
        yield b""


class _RecordingHandler(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.requests.append(
            {"path": self.path, "json": json.loads(body) if body else None}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def stub_orchestrator():
    _RecordingHandler.requests = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}", _RecordingHandler.requests
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def dispatcher():
    """Module-scoped: aiogram's `router` (the module-level singleton from
    services.telegram_gateway.bot) can only ever be attached to one parent
    Dispatcher for its lifetime -- a function-scoped fixture rebuilding a
    fresh Dispatcher per test would try to re-attach the same router a
    second time and hit aiogram's "Router is already attached" RuntimeError.
    One Dispatcher shared across this module's tests also matches how the
    real Telegram Gateway runs: one process, one Dispatcher, many updates."""
    dp = Dispatcher()
    auth_middleware = AllowedUserMiddleware(ALLOWED_USER_ID)
    dp.message.outer_middleware(auth_middleware)
    dp.callback_query.outer_middleware(auth_middleware)
    dp.include_router(router)
    return dp


@pytest.fixture
def bot():
    return Bot(token="123456:test-token", session=NullTelegramSession())


def _build_message_update(update_id: int, user_id: int, text: str) -> Update:
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }
    return Update.model_validate(payload)


def test_authorized_message_reaches_stub_orchestrator(stub_orchestrator, dispatcher, bot):
    base_url, requests = stub_orchestrator
    update = _build_message_update(1, ALLOWED_USER_ID, "hello foundry")

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            orchestrator = OrchestratorClient(base_url, client)
            poller = ProgressPoller(orchestrator, bot)
            await dispatcher.feed_update(bot, update, orchestrator=orchestrator, poller=poller)

    asyncio.run(scenario())

    assert len(requests) == 1
    assert requests[0]["path"] == "/webhook"
    assert requests[0]["json"] == {
        "user_id": ALLOWED_USER_ID,
        "text": "hello foundry",
        "force_new": False,
    }


def test_unauthorized_message_never_reaches_stub_orchestrator(stub_orchestrator, dispatcher, bot):
    base_url, requests = stub_orchestrator
    update = _build_message_update(1, OTHER_USER_ID, "hello foundry")

    async def scenario() -> None:
        async with httpx.AsyncClient() as client:
            orchestrator = OrchestratorClient(base_url, client)
            poller = ProgressPoller(orchestrator, bot)
            await dispatcher.feed_update(bot, update, orchestrator=orchestrator, poller=poller)

    asyncio.run(scenario())

    assert requests == []
