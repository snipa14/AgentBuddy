"""Unit tests for OrchestratorClient."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx

from services.telegram_gateway.orchestrator_client import OrchestratorClient


def _mock_httpx_client() -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    return client


def test_send_webhook_posts_user_id_and_text():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.send_webhook(42, "hello"))

    args, kwargs = client.post.call_args
    assert args[0] == "http://orchestrator:8000/webhook"
    assert kwargs["json"] == {"user_id": 42, "text": "hello"}


def test_resume_posts_decision_to_thread_specific_url():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000/", client)

    asyncio.run(orchestrator.resume("thread-1", "approve"))

    args, kwargs = client.post.call_args
    assert args[0] == "http://orchestrator:8000/resume/thread-1"
    assert kwargs["json"] == {"decision": "approve"}
