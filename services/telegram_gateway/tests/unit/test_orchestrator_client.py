"""Unit tests for OrchestratorClient."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx

from services.telegram_gateway.orchestrator_client import OrchestratorClient


def _mock_httpx_client() -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.post = AsyncMock()
    client.get = AsyncMock()
    return client


def test_send_webhook_posts_user_id_and_text():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.send_webhook(42, "hello"))

    args, kwargs = client.post.call_args
    assert args[0] == "http://orchestrator:8000/webhook"
    assert kwargs["json"] == {"user_id": 42, "text": "hello", "force_new": False}


def test_send_webhook_can_force_a_new_project():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.send_webhook(42, "hello", force_new=True))

    _, kwargs = client.post.call_args
    assert kwargs["json"] == {"user_id": 42, "text": "hello", "force_new": True}


def test_resume_posts_decision_to_thread_specific_url():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000/", client)

    asyncio.run(orchestrator.resume("thread-1", "approve"))

    args, kwargs = client.post.call_args
    assert args[0] == "http://orchestrator:8000/resume/thread-1"
    assert kwargs["json"] == {"decision": "approve"}


def test_stream_gets_since_seq_as_a_query_param():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.stream("project-1", since_seq=3))

    args, kwargs = client.get.call_args
    assert args[0] == "http://orchestrator:8000/stream/project-1"
    assert kwargs["params"] == {"since_seq": 3}


def test_stream_defaults_since_seq_to_everything():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.stream("project-1"))

    _, kwargs = client.get.call_args
    assert kwargs["params"] == {"since_seq": -1}


def test_get_status_gets_the_project_specific_url():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000/", client)

    asyncio.run(orchestrator.get_status("project-1"))

    args, _ = client.get.call_args
    assert args[0] == "http://orchestrator:8000/status/project-1"


def test_get_projects_gets_the_projects_url():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.get_projects())

    args, _ = client.get.call_args
    assert args[0] == "http://orchestrator:8000/projects"


def test_activate_posts_to_the_project_specific_url():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000/", client)

    asyncio.run(orchestrator.activate("proj-1"))

    args, _ = client.post.call_args
    assert args[0] == "http://orchestrator:8000/activate/proj-1"


def test_rename_posts_the_new_name_to_the_rename_url():
    client = _mock_httpx_client()
    orchestrator = OrchestratorClient("http://orchestrator:8000", client)

    asyncio.run(orchestrator.rename("CSV Converter"))

    args, kwargs = client.post.call_args
    assert args[0] == "http://orchestrator:8000/rename"
    assert kwargs["json"] == {"name": "CSV Converter"}
