"""Unit tests for ExecutorClient.

Uses httpx.MockTransport to fake the Executor's HTTP responses in-process --
no real server, no mocked internals, just the actual httpx request/response
path exercised against a canned handler.
"""

import asyncio

import httpx
import pytest

from services.orchestrator.executor_client import ExecutorClient, ExecutorClientError


def _client_with(handler) -> ExecutorClient:
    transport = httpx.MockTransport(handler)
    return ExecutorClient("http://executor:8000", httpx.AsyncClient(transport=transport))


def test_run_command_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/run_command"
        assert request.method == "POST"
        return httpx.Response(200, json={"stdout": "hi\n", "stderr": "", "exit_code": 0})

    client = _client_with(handler)

    result = asyncio.run(client.run_command("proj-1", ["echo", "hi"]))

    assert result.stdout == "hi\n"
    assert result.exit_code == 0


def test_read_file_returns_content():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/read_file"
        return httpx.Response(200, json={"content": "hello"})

    client = _client_with(handler)

    content = asyncio.run(client.read_file("proj-1", "a.txt"))

    assert content == "hello"


def test_read_file_raises_executor_client_error_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    client = _client_with(handler)

    with pytest.raises(ExecutorClientError, match="404"):
        asyncio.run(client.read_file("proj-1", "missing.txt"))


def test_write_file_returns_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/write_file"
        return httpx.Response(200, json={"ok": True})

    client = _client_with(handler)

    ok = asyncio.run(client.write_file("proj-1", "a.txt", "content"))

    assert ok is True


def test_list_files_parses_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"entries": [{"name": "a.txt", "is_dir": False}]}
        )

    client = _client_with(handler)

    entries = asyncio.run(client.list_files("proj-1"))

    assert entries[0].name == "a.txt"
    assert entries[0].is_dir is False


def test_run_tests_parses_result():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"passed": 5, "failed": 0, "coverage_pct": 92.5, "report": "ok"}
        )

    client = _client_with(handler)

    result = asyncio.run(client.run_tests("proj-1"))

    assert result.passed == 5
    assert result.coverage_pct == 92.5


def test_run_command_raises_executor_client_error_on_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = _client_with(handler)

    with pytest.raises(ExecutorClientError, match="500"):
        asyncio.run(client.run_command("proj-1", ["ls"]))
