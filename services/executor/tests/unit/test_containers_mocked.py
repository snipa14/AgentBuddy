"""Unit tests for containers.py's error-handling and cleanup branches.

The happy path is already exercised indirectly by tests/unit/test_tools_mocked.py;
these specifically target the branches only a failure hits: container creation
failing, remove() itself failing during cleanup, exec_run raising a
non-timeout APIError, and the timeout path killing the container.
"""

import time
from unittest.mock import MagicMock

import pytest
from docker.errors import APIError
from docker.models.containers import Container

from services.executor.config import ExecutorConfig
from services.executor.containers import exec_in_container, workspace_container
from services.executor.errors import CommandTimeoutError, ContainerExecutionError


@pytest.fixture
def config(tmp_path):
    return ExecutorConfig(projects_host_path=tmp_path)


def test_workspace_container_raises_container_execution_error_when_run_fails(config, tmp_path):
    client = MagicMock()
    client.containers.run.side_effect = APIError("no such image")

    with pytest.raises(ContainerExecutionError):
        with workspace_container(client, config, tmp_path):
            pass


def test_workspace_container_swallows_remove_failure_during_cleanup(config, tmp_path):
    mock_container = MagicMock(spec=Container)
    mock_container.remove.side_effect = APIError("already gone")
    client = MagicMock()
    client.containers.run.return_value = mock_container

    with workspace_container(client, config, tmp_path) as container:
        assert container is mock_container

    mock_container.remove.assert_called_once_with(force=True)


def test_exec_in_container_raises_container_execution_error_on_api_error():
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = APIError("exec failed")

    with pytest.raises(ContainerExecutionError):
        exec_in_container(mock_container, ["echo", "hi"], timeout=5)


def test_exec_in_container_kills_container_and_raises_on_timeout():
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = lambda *a, **kw: time.sleep(0.2)

    with pytest.raises(CommandTimeoutError):
        exec_in_container(mock_container, ["sleep", "30"], timeout=0.01)

    mock_container.kill.assert_called_once()
