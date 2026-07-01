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
from services.executor.errors import PathTraversalError
from services.executor.tools import (
    tool_list_files,
    tool_read_file,
    tool_run_command,
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
