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
from services.executor.containers import (
    _install_dependencies_if_present,
    exec_in_container,
    workspace_container,
)
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


def test_workspace_container_mounts_python_cache_volume_at_pip_cache_path(config, tmp_path):
    mock_container = MagicMock(spec=Container)
    client = MagicMock()
    client.containers.run.return_value = mock_container

    with workspace_container(client, config, tmp_path, stack="python", project_id="proj-1"):
        pass

    _, run_kwargs = client.containers.run.call_args
    assert run_kwargs["volumes"]["foundry-cache-proj-1"] == {
        "bind": "/home/sandbox/.cache/pip",
        "mode": "rw",
    }


def test_workspace_container_mounts_node_cache_volume_at_npm_cache_path(config, tmp_path):
    mock_container = MagicMock(spec=Container)
    client = MagicMock()
    client.containers.run.return_value = mock_container

    with workspace_container(client, config, tmp_path, stack="node", project_id="proj-1"):
        pass

    _, run_kwargs = client.containers.run.call_args
    assert run_kwargs["volumes"]["foundry-cache-proj-1"] == {
        "bind": "/home/sandbox/.npm",
        "mode": "rw",
    }


def test_workspace_container_omits_cache_volume_for_a_stack_with_none_registered(config, tmp_path):
    mock_container = MagicMock(spec=Container)
    client = MagicMock()
    client.containers.run.return_value = mock_container

    with workspace_container(client, config, tmp_path, stack="ruby", project_id="proj-1"):
        pass

    _, run_kwargs = client.containers.run.call_args
    assert len(run_kwargs["volumes"]) == 1
    assert "foundry-cache-proj-1" not in run_kwargs["volumes"]


def test_install_dependencies_if_present_runs_pip_install_when_requirements_txt_exists():
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = [(0, (b"", b"")), (0, (b"installed\n", b""))]

    _install_dependencies_if_present(mock_container, "python", timeout=5)

    calls = mock_container.exec_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["test", "-f", "requirements.txt"]
    assert calls[1].args[0] == ["pip", "install", "--user", "-r", "requirements.txt"]


def test_install_dependencies_if_present_falls_back_to_pyproject_toml():
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = [
        (1, (b"", b"")),
        (0, (b"", b"")),
        (0, (b"installed\n", b"")),
    ]

    _install_dependencies_if_present(mock_container, "python", timeout=5)

    calls = mock_container.exec_run.call_args_list
    assert len(calls) == 3
    assert calls[0].args[0] == ["test", "-f", "requirements.txt"]
    assert calls[1].args[0] == ["test", "-f", "pyproject.toml"]
    assert calls[2].args[0] == ["pip", "install", "--user", "-e", "."]


def test_install_dependencies_if_present_runs_npm_install_when_package_json_exists():
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = [(0, (b"", b"")), (0, (b"installed\n", b""))]

    _install_dependencies_if_present(mock_container, "node", timeout=5)

    calls = mock_container.exec_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["test", "-f", "package.json"]
    assert calls[1].args[0] == ["npm", "install"]


def test_install_dependencies_if_present_skips_install_when_no_manifest_is_present():
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.return_value = (1, (b"", b""))

    _install_dependencies_if_present(mock_container, "python", timeout=5)

    calls = mock_container.exec_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["test", "-f", "requirements.txt"]
    assert calls[1].args[0] == ["test", "-f", "pyproject.toml"]


def test_install_dependencies_if_present_is_a_noop_for_a_stack_without_a_manifest():
    mock_container = MagicMock(spec=Container)

    _install_dependencies_if_present(mock_container, "ruby", timeout=5)

    mock_container.exec_run.assert_not_called()
