"""Unit tests for the Executor's tool_* functions, with the Docker client mocked.

These verify the Executor's own logic (path validation, container lifecycle
wiring, the security kwargs passed to the Docker SDK) -- NOT that the
security boundary actually holds. Per .claude/rules/security.md, only a real
Docker daemon (tests/integration/test_executor_sandbox.py) proves that.
"""

import json
from unittest.mock import MagicMock

import pytest
from docker.errors import NotFound
from docker.models.containers import Container

from services.executor.config import ExecutorConfig
from services.executor.errors import ContainerExecutionError, PathTraversalError
from services.executor.tools import (
    _build_single_file_tar,
    tool_list_files,
    tool_read_file,
    tool_run_command,
    tool_run_tests,
    tool_take_screenshot,
    tool_write_file,
)


@pytest.fixture
def config(tmp_path):
    return ExecutorConfig(projects_host_path=tmp_path)


@pytest.fixture
def mock_container():
    return MagicMock(spec=Container)


@pytest.fixture
def mock_client(mock_container):
    client = MagicMock()
    client.containers.run.return_value = mock_container
    return client


def test_tool_run_command_creates_locked_down_container_and_removes_it(
    mock_client, mock_container, config
):
    mock_container.exec_run.return_value = (0, (b"hello\n", b""))

    result = tool_run_command(mock_client, config, "proj-1", ["echo", "hello"])

    assert result.exit_code == 0
    assert result.stdout == "hello\n"

    _, run_kwargs = mock_client.containers.run.call_args
    assert run_kwargs["cap_drop"] == ["ALL"]
    assert run_kwargs["security_opt"] == ["no-new-privileges:true"]
    assert run_kwargs["mem_limit"] == config.mem_limit
    assert run_kwargs["nano_cpus"] == config.nano_cpus
    assert run_kwargs["network"] == config.network_name
    assert run_kwargs["user"] == "1000:1000"
    mount_source = str((config.projects_host_path / "proj-1").resolve())
    assert run_kwargs["volumes"][mount_source] == {"bind": "/workspace", "mode": "rw"}

    mock_container.remove.assert_called_once_with(force=True)


def test_tool_run_command_removes_container_even_on_exec_error(
    mock_client, mock_container, config
):
    mock_container.exec_run.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        tool_run_command(mock_client, config, "proj-1", ["false"])

    mock_container.remove.assert_called_once_with(force=True)


def test_tool_run_command_rejects_project_id_path_traversal(mock_client, config):
    with pytest.raises(PathTraversalError):
        tool_run_command(mock_client, config, "../../etc", ["echo", "hi"])

    mock_client.containers.run.assert_not_called()


def test_tool_read_file_rejects_path_traversal_without_touching_docker(mock_client, config):
    with pytest.raises(PathTraversalError):
        tool_read_file(mock_client, config, "proj-1", "../../etc/passwd")

    mock_client.containers.run.assert_not_called()


def test_tool_write_file_rejects_path_traversal_without_touching_docker(mock_client, config):
    with pytest.raises(PathTraversalError):
        tool_write_file(mock_client, config, "proj-1", "../../etc/passwd", "data")

    mock_client.containers.run.assert_not_called()


def test_tool_list_files_rejects_path_traversal_without_touching_docker(mock_client, config):
    with pytest.raises(PathTraversalError):
        tool_list_files(mock_client, config, "proj-1", "../outside")

    mock_client.containers.run.assert_not_called()


def test_tool_list_files_parses_json_output(mock_client, mock_container, config):
    payload = json.dumps([{"name": "app.py", "is_dir": False}]).encode() + b"\n"
    mock_container.exec_run.return_value = (0, (payload, b""))

    entries = tool_list_files(mock_client, config, "proj-1", ".")

    assert len(entries) == 1
    assert entries[0].name == "app.py"
    assert entries[0].is_dir is False


def test_tool_read_file_raises_file_not_found_error_when_missing(
    mock_client, mock_container, config
):
    mock_container.get_archive.side_effect = NotFound("no such path")

    with pytest.raises(FileNotFoundError):
        tool_read_file(mock_client, config, "proj-1", "missing.py")


def test_tool_write_file_creates_parent_directory_and_puts_archive(
    mock_client, mock_container, config
):
    tool_write_file(mock_client, config, "proj-1", "src/app.py", "print('hi')")

    mock_container.exec_run.assert_called_once_with(["mkdir", "-p", "/workspace/src"])
    put_args, _ = mock_container.put_archive.call_args
    assert put_args[0] == "/workspace/src"


def test_tool_read_file_returns_content_from_get_archive_tar_stream(
    mock_client, mock_container, config
):
    tar_bytes = _build_single_file_tar("existing.txt", "already here")
    mock_container.get_archive.return_value = ([tar_bytes], {"name": "existing.txt"})

    content = tool_read_file(mock_client, config, "proj-1", "existing.txt")

    assert content == "already here"


def test_tool_list_files_raises_container_execution_error_on_nonzero_exit(
    mock_client, mock_container, config
):
    mock_container.exec_run.return_value = (1, (b"", b"permission denied"))

    with pytest.raises(ContainerExecutionError):
        tool_list_files(mock_client, config, "proj-1", ".")


def test_tool_run_tests_rejects_unsupported_framework(mock_client, config):
    with pytest.raises(ValueError):
        tool_run_tests(mock_client, config, "proj-1", framework="unittest")

    mock_client.containers.run.assert_not_called()


def test_tool_run_tests_parses_passed_failed_and_coverage(mock_client, mock_container, config):
    pytest_output = (
        "Name       Stmts   Miss  Cover\n"
        "TOTAL          10      1    90%\n"
        "3 passed, 1 failed in 0.30s\n"
    )
    mock_container.exec_run.return_value = (1, (pytest_output.encode(), b""))

    result = tool_run_tests(mock_client, config, "proj-1")

    assert result.passed == 3
    assert result.failed == 1
    assert result.coverage_pct == 90.0
    assert pytest_output in result.report


def test_tool_take_screenshot_returns_image_b64_on_success(mock_client, mock_container, config):
    mock_container.exec_run.return_value = (0, (b"iVBORw0KGgo=\n", b""))

    result = tool_take_screenshot(mock_client, config, "proj-1")

    assert result.image_b64 == "iVBORw0KGgo="
    assert result.error is None


def test_tool_take_screenshot_returns_error_on_nonzero_exit(mock_client, mock_container, config):
    mock_container.exec_run.return_value = (1, (b"", b"ERROR:url_not_ready\n"))

    result = tool_take_screenshot(mock_client, config, "proj-1")

    assert result.image_b64 == ""
    assert "url_not_ready" in result.error


def test_tool_take_screenshot_returns_error_on_empty_stdout(mock_client, mock_container, config):
    mock_container.exec_run.return_value = (0, (b"", b""))

    result = tool_take_screenshot(mock_client, config, "proj-1")

    assert result.image_b64 == ""
    assert result.error == "screenshot failed"


def test_tool_take_screenshot_passes_auto_start_flag_to_the_container_command(
    mock_client, mock_container, config
):
    mock_container.exec_run.return_value = (0, (b"iVBORw0KGgo=\n", b""))

    tool_take_screenshot(mock_client, config, "proj-1", auto_start_server=True)

    command = mock_container.exec_run.call_args.args[0]
    assert "true" in command
