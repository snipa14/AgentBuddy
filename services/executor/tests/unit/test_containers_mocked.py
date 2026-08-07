"""Unit tests for containers.py's error-handling and cleanup branches.

The happy path is already exercised indirectly by tests/unit/test_tools_mocked.py;
these specifically target the branches only a failure hits: container creation
failing, remove() itself failing during cleanup, exec_run raising a
non-timeout APIError, and the timeout path killing the container.
"""

import logging
import time
from unittest.mock import MagicMock

import pytest
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from services.executor.config import ExecutorConfig
from services.executor.containers import (
    _install_dependencies_if_present,
    exec_in_container,
    workspace_container,
)
from services.executor.errors import CommandTimeoutError, ContainerExecutionError
from services.executor.stacks import StackConfig


@pytest.fixture
def config(tmp_path):
    return ExecutorConfig(projects_host_path=tmp_path)


def test_workspace_container_raises_container_execution_error_when_create_fails(config, tmp_path):
    client = MagicMock()
    client.containers.create.side_effect = APIError("no such image")

    with pytest.raises(ContainerExecutionError):
        with workspace_container(client, config, tmp_path):
            pass


def test_workspace_container_swallows_remove_failure_during_cleanup(config, tmp_path):
    mock_container = MagicMock(spec=Container)
    mock_container.remove.side_effect = APIError("already gone")
    client = MagicMock()
    client.containers.create.return_value = mock_container

    with workspace_container(client, config, tmp_path) as container:
        assert container is mock_container

    mock_container.remove.assert_called_once_with(force=True)


def test_workspace_container_removes_a_container_that_was_created_but_never_started(
    config, tmp_path
):
    """The exact leak reported in production: create() succeeds (a real
    container now exists on the daemon) but start() then fails -- the
    container must still be captured and removed, not silently orphaned
    because the combined run() call raised before returning anything."""
    mock_container = MagicMock(spec=Container)
    mock_container.start.side_effect = APIError("404 Client Error: no such container")
    client = MagicMock()
    client.containers.create.return_value = mock_container

    with pytest.raises(ContainerExecutionError):
        with workspace_container(client, config, tmp_path):
            pass

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


def test_exec_in_container_raises_command_timeout_error_when_kill_also_fails(caplog):
    """If the daemon has already reaped the container by the time the timeout
    handler calls kill() (a race under load, or the daemon's own cleanup),
    kill() itself raises APIError. The caller still only ever needs to know
    the command didn't finish in time -- CommandTimeoutError must still be
    raised, never the raw APIError, and the swallowed APIError should be
    logged rather than silently dropped."""
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = lambda *a, **kw: time.sleep(0.2)
    mock_container.kill.side_effect = APIError("no such container")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CommandTimeoutError):
            exec_in_container(mock_container, ["sleep", "30"], timeout=0.01)

    mock_container.kill.assert_called_once()
    assert any("no such container" in record.message for record in caplog.records)


def test_exec_in_container_logs_kill_not_found_at_debug_not_warning(caplog):
    """A kill() failure meaning "the container is already gone" (NotFound) is
    an expected race under load or the daemon's own cleanup -- it should log
    at debug, not warning, unlike a genuine kill() failure."""
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = lambda *a, **kw: time.sleep(0.2)
    mock_container.kill.side_effect = NotFound("no such container")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CommandTimeoutError):
            exec_in_container(mock_container, ["sleep", "30"], timeout=0.01)

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("already reaped" in r.message for r in debug_records)
    assert warning_records == []


def test_exec_in_container_does_not_block_on_slow_background_thread_after_timeout():
    """Regression test for the ThreadPoolExecutor context-manager hang: its
    __exit__ blocks (wait=True) until the submitted future finishes, which
    would defeat the timeout entirely if the background exec_run call
    doesn't unblock promptly after kill() (e.g. a stalled daemon/proxy HTTP
    call). exec_in_container must return once it gives up at `timeout`, not
    once the background thread eventually finishes."""
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = lambda *a, **kw: time.sleep(2)

    start = time.monotonic()
    with pytest.raises(CommandTimeoutError):
        exec_in_container(mock_container, ["sleep", "30"], timeout=0.05)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0


def test_workspace_container_mounts_python_cache_volume_at_pip_cache_path(
    two_stack_registry, tmp_path
):
    config = ExecutorConfig(projects_host_path=tmp_path, stacks=two_stack_registry)
    mock_container = MagicMock(spec=Container)
    client = MagicMock()
    client.containers.create.return_value = mock_container

    with workspace_container(client, config, tmp_path, stack="python", project_id="proj-1"):
        pass

    _, create_kwargs = client.containers.create.call_args
    assert create_kwargs["volumes"]["foundry-cache-proj-1-python"] == {
        "bind": "/home/sandbox/.cache/pip",
        "mode": "rw",
    }


def test_workspace_container_mounts_node_cache_volume_at_npm_cache_path(
    two_stack_registry, tmp_path
):
    config = ExecutorConfig(projects_host_path=tmp_path, stacks=two_stack_registry)
    mock_container = MagicMock(spec=Container)
    client = MagicMock()
    client.containers.create.return_value = mock_container

    with workspace_container(client, config, tmp_path, stack="node", project_id="proj-1"):
        pass

    _, create_kwargs = client.containers.create.call_args
    assert create_kwargs["volumes"]["foundry-cache-proj-1-node"] == {
        "bind": "/home/sandbox/.npm",
        "mode": "rw",
    }


def test_workspace_container_uses_distinct_cache_volumes_for_different_stacks_same_project(
    two_stack_registry, tmp_path
):
    """A project keyed by project_id alone (not project_id+stack) would reuse
    one stack's package-manager cache volume for another stack's cache_dir
    mount, mixing pip and npm cache contents at the wrong path (ADR-028's
    "per-project, per-stack" cache volume design)."""
    config = ExecutorConfig(projects_host_path=tmp_path, stacks=two_stack_registry)
    client = MagicMock()

    client.containers.create.return_value = MagicMock(spec=Container)
    with workspace_container(client, config, tmp_path, stack="python", project_id="proj-1"):
        pass
    _, python_kwargs = client.containers.create.call_args

    client.containers.create.return_value = MagicMock(spec=Container)
    with workspace_container(client, config, tmp_path, stack="node", project_id="proj-1"):
        pass
    _, node_kwargs = client.containers.create.call_args

    python_volume_names = set(python_kwargs["volumes"]) - {str(tmp_path)}
    node_volume_names = set(node_kwargs["volumes"]) - {str(tmp_path)}
    assert python_volume_names.isdisjoint(node_volume_names)


def test_workspace_container_omits_cache_volume_for_a_stack_with_none_registered(
    two_stack_registry, tmp_path
):
    config = ExecutorConfig(projects_host_path=tmp_path, stacks=two_stack_registry)
    mock_container = MagicMock(spec=Container)
    client = MagicMock()
    client.containers.create.return_value = mock_container

    with workspace_container(client, config, tmp_path, stack="ruby", project_id="proj-1"):
        pass

    _, create_kwargs = client.containers.create.call_args
    assert len(create_kwargs["volumes"]) == 1
    assert "foundry-cache-proj-1-ruby" not in create_kwargs["volumes"]


def test_workspace_container_unknown_stack_gets_only_the_project_bind_mount(config, tmp_path):
    mock_container = MagicMock(spec=Container)
    client = MagicMock()
    client.containers.create.return_value = mock_container

    with workspace_container(client, config, tmp_path, stack="cobol", project_id="proj-1"):
        pass

    _, create_kwargs = client.containers.create.call_args
    assert len(create_kwargs["volumes"]) == 1
    mount_source = str(tmp_path)
    assert create_kwargs["volumes"][mount_source] == {"bind": "/workspace", "mode": "rw"}


def test_install_dependencies_if_present_runs_pip_install_when_requirements_txt_exists(
    two_stack_registry,
):
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = [(0, (b"", b"")), (0, (b"installed\n", b""))]

    _install_dependencies_if_present(
        mock_container, two_stack_registry.stacks["python"], timeout=5
    )

    calls = mock_container.exec_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["test", "-f", "requirements.txt"]
    assert calls[1].args[0] == ["pip", "install", "--user", "-r", "requirements.txt"]


def test_install_dependencies_if_present_falls_back_to_pyproject_toml(two_stack_registry):
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = [
        (1, (b"", b"")),
        (0, (b"", b"")),
        (0, (b"installed\n", b"")),
    ]

    _install_dependencies_if_present(
        mock_container, two_stack_registry.stacks["python"], timeout=5
    )

    calls = mock_container.exec_run.call_args_list
    assert len(calls) == 3
    assert calls[0].args[0] == ["test", "-f", "requirements.txt"]
    assert calls[1].args[0] == ["test", "-f", "pyproject.toml"]
    assert calls[2].args[0] == ["pip", "install", "--user", "-e", "."]


def test_install_dependencies_if_present_runs_npm_install_when_package_json_exists(
    two_stack_registry,
):
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = [(0, (b"", b"")), (0, (b"installed\n", b""))]

    _install_dependencies_if_present(mock_container, two_stack_registry.stacks["node"], timeout=5)

    calls = mock_container.exec_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["test", "-f", "package.json"]
    assert calls[1].args[0] == ["npm", "install"]


def test_install_dependencies_if_present_skips_install_when_no_manifest_is_present(
    two_stack_registry,
):
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.return_value = (1, (b"", b""))

    _install_dependencies_if_present(
        mock_container, two_stack_registry.stacks["python"], timeout=5
    )

    calls = mock_container.exec_run.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == ["test", "-f", "requirements.txt"]
    assert calls[1].args[0] == ["test", "-f", "pyproject.toml"]


def test_install_dependencies_if_present_raises_when_install_command_fails(two_stack_registry):
    """A failed pip/npm install must not be swallowed -- letting the caller's
    subsequent command run against a container with missing dependencies
    surfaces as a confusing downstream error instead of the real cause."""
    mock_container = MagicMock(spec=Container)
    mock_container.exec_run.side_effect = [
        (0, (b"", b"")),
        (1, (b"", b"ERROR: could not find a version that satisfies foo\n")),
    ]

    with pytest.raises(ContainerExecutionError, match="dependency install failed"):
        _install_dependencies_if_present(
            mock_container, two_stack_registry.stacks["python"], timeout=5
        )


def test_install_dependencies_if_present_is_a_noop_for_a_stack_without_a_manifest():
    mock_container = MagicMock(spec=Container)
    stack_entry = StackConfig(image="foundry/workspace-ruby:3", base_image="ruby:3-slim")

    _install_dependencies_if_present(mock_container, stack_entry, timeout=5)

    mock_container.exec_run.assert_not_called()


def test_workspace_container_logs_create_start_and_remove(config, tmp_path, caplog):
    mock_container = MagicMock(spec=Container)
    mock_container.id = "abc123def456ff00extra"
    client = MagicMock()
    client.containers.create.return_value = mock_container

    with caplog.at_level(logging.INFO):
        with workspace_container(client, config, tmp_path, stack="python", project_id="proj-1"):
            pass

    messages = [record.message for record in caplog.records]
    assert any("proj-1" in m and "creating workspace container" in m for m in messages)
    assert any("proj-1" in m and "abc123def456" in m and "started" in m for m in messages)
    assert any("proj-1" in m and "abc123def456" in m and "removing" in m for m in messages)
    assert not any("abc123def456ff00extra" in m for m in messages)
