"""Unit tests for the Executor's FastAPI surface.

Mocks the tool_* functions themselves (already covered by test_tools_mocked.py
and test_tools_parsing.py) -- these tests are only about HTTP routing and
error-status mapping, not tool correctness.
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import services.executor.api as api_module
from services.executor.config import ExecutorConfig
from services.executor.errors import CommandTimeoutError, ContainerExecutionError
from services.executor.schemas import CommandResult, FileEntry, ScreenshotResult, TestResult


@pytest.fixture
def client(tmp_path):
    config = ExecutorConfig(projects_host_path=tmp_path)
    app = api_module.build_app(MagicMock(), config)
    return TestClient(app)


def test_run_command_returns_result(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "tool_run_command",
        lambda *args, **kwargs: CommandResult(stdout="hi", stderr="", exit_code=0),
    )

    response = client.post(
        "/run_command", json={"project_id": "proj-1", "command": ["echo", "hi"]}
    )

    assert response.status_code == 200
    assert response.json() == {"stdout": "hi", "stderr": "", "exit_code": 0}


def test_run_command_returns_400_on_value_error(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ValueError("bad path")

    monkeypatch.setattr(api_module, "tool_run_command", _raise)

    response = client.post("/run_command", json={"project_id": "proj-1", "command": ["ls"]})

    assert response.status_code == 400


def test_run_command_returns_504_on_timeout(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise CommandTimeoutError("timed out")

    monkeypatch.setattr(api_module, "tool_run_command", _raise)

    response = client.post("/run_command", json={"project_id": "proj-1", "command": ["sleep", "1"]})

    assert response.status_code == 504


def test_run_command_returns_500_on_container_error(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ContainerExecutionError("boom")

    monkeypatch.setattr(api_module, "tool_run_command", _raise)

    response = client.post("/run_command", json={"project_id": "proj-1", "command": ["ls"]})

    assert response.status_code == 500


def test_read_file_returns_content(client, monkeypatch):
    monkeypatch.setattr(api_module, "tool_read_file", lambda *args, **kwargs: "file contents")

    response = client.post("/read_file", json={"project_id": "proj-1", "path": "a.txt"})

    assert response.status_code == 200
    assert response.json() == {"content": "file contents"}


def test_read_file_returns_404_when_missing(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(api_module, "tool_read_file", _raise)

    response = client.post("/read_file", json={"project_id": "proj-1", "path": "a.txt"})

    assert response.status_code == 404


def test_read_file_returns_400_on_path_traversal(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ValueError("path escapes project root")

    monkeypatch.setattr(api_module, "tool_read_file", _raise)

    response = client.post("/read_file", json={"project_id": "proj-1", "path": "../etc/passwd"})

    assert response.status_code == 400


def test_write_file_returns_ok(client, monkeypatch):
    monkeypatch.setattr(api_module, "tool_write_file", lambda *args, **kwargs: True)

    response = client.post(
        "/write_file", json={"project_id": "proj-1", "path": "a.txt", "content": "hi"}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_write_file_returns_400_on_path_traversal(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ValueError("path escapes project root")

    monkeypatch.setattr(api_module, "tool_write_file", _raise)

    response = client.post(
        "/write_file", json={"project_id": "proj-1", "path": "../x", "content": "hi"}
    )

    assert response.status_code == 400


def test_list_files_returns_entries(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "tool_list_files",
        lambda *args, **kwargs: [FileEntry(name="a.txt", is_dir=False)],
    )

    response = client.post("/list_files", json={"project_id": "proj-1"})

    assert response.status_code == 200
    assert response.json() == {"entries": [{"name": "a.txt", "is_dir": False}]}


def test_list_files_returns_400_on_path_traversal(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ValueError("path escapes project root")

    monkeypatch.setattr(api_module, "tool_list_files", _raise)

    response = client.post("/list_files", json={"project_id": "proj-1", "path": "../x"})

    assert response.status_code == 400


def test_list_files_returns_500_on_container_error(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ContainerExecutionError("boom")

    monkeypatch.setattr(api_module, "tool_list_files", _raise)

    response = client.post("/list_files", json={"project_id": "proj-1"})

    assert response.status_code == 500


def test_run_tests_returns_result(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "tool_run_tests",
        lambda *args, **kwargs: TestResult(passed=3, failed=0, coverage_pct=91.0, report="ok"),
    )

    response = client.post("/run_tests", json={"project_id": "proj-1"})

    assert response.status_code == 200
    assert response.json()["passed"] == 3


def test_run_tests_returns_400_on_unsupported_framework(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ValueError("unsupported test framework: 'jest'")

    monkeypatch.setattr(api_module, "tool_run_tests", _raise)

    response = client.post("/run_tests", json={"project_id": "proj-1", "framework": "jest"})

    assert response.status_code == 400


def test_take_screenshot_returns_result(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "tool_take_screenshot",
        lambda *args, **kwargs: ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
    )

    response = client.post("/take_screenshot", json={"project_id": "proj-1"})

    assert response.status_code == 200
    assert response.json() == {"image_b64": "iVBORw0KGgo=", "error": None}


def test_take_screenshot_passes_request_fields_through(client, monkeypatch):
    captured = {}

    def _capture(client_, config, project_id, url, auto_start_server, breakpoint_width, stack):
        captured.update(
            project_id=project_id,
            url=url,
            auto_start_server=auto_start_server,
            breakpoint_width=breakpoint_width,
            stack=stack,
        )
        return ScreenshotResult(image_b64="", error=None)

    monkeypatch.setattr(api_module, "tool_take_screenshot", _capture)

    response = client.post(
        "/take_screenshot",
        json={
            "project_id": "proj-1",
            "url": "http://localhost:3000",
            "auto_start_server": True,
            "breakpoint_width": 375,
        },
    )

    assert response.status_code == 200
    assert captured == {
        "project_id": "proj-1",
        "url": "http://localhost:3000",
        "auto_start_server": True,
        "breakpoint_width": 375,
        "stack": "python",
    }


def test_take_screenshot_returns_400_on_value_error(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ValueError("bad request")

    monkeypatch.setattr(api_module, "tool_take_screenshot", _raise)

    response = client.post("/take_screenshot", json={"project_id": "proj-1"})

    assert response.status_code == 400


def test_clear_project_cache_returns_ok(client, monkeypatch):
    captured = {}

    def _capture(client_, config, project_id, stack):
        captured.update(project_id=project_id, stack=stack)
        return True

    monkeypatch.setattr(api_module, "tool_clear_project_cache", _capture)

    response = client.post(
        "/clear_project_cache", json={"project_id": "proj-1", "stack": "node"}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured == {"project_id": "proj-1", "stack": "node"}


def test_clear_project_cache_returns_500_on_container_error(client, monkeypatch):
    def _raise(*args, **kwargs):
        raise ContainerExecutionError("boom")

    monkeypatch.setattr(api_module, "tool_clear_project_cache", _raise)

    response = client.post(
        "/clear_project_cache", json={"project_id": "proj-1", "stack": "python"}
    )

    assert response.status_code == 500
