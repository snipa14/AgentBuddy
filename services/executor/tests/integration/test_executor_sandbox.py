"""Integration tests proving the Docker security boundary actually holds.

Requires a real, reachable Docker daemon. Per .claude/rules/security.md:
"unittest.mock.patch('docker.from_env')" tests are fine for Executor *logic*
(see tests/unit/) but prove nothing about containment -- only a live daemon
does. These tests use docker.from_env() directly against whatever daemon is
reachable from wherever pytest runs (a developer's local Docker, not
necessarily through docker-socket-proxy -- the proxy's narrow whitelist is
a production concern for the Executor's own runtime wiring, not something
a local test run needs to route through).

Run with: pytest -m integration services/executor/tests/integration
"""

import uuid
from pathlib import Path

import docker
import pytest

from services.executor.config import DEFAULT_STACKS_CONFIG_PATH, ExecutorConfig
from services.executor.docker_client import build_docker_client
from services.executor.errors import CommandTimeoutError
from services.executor.stacks import load_stacks_config
from services.executor.tools import tool_read_file, tool_run_command, tool_write_file

WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DOCKERFILE_DIR = "images/workspace-python"
NODE_WORKSPACE_IMAGE = "foundry/workspace-node:20"
NODE_DOCKERFILE_DIR = "images/workspace-node"
TEST_NETWORK_NAME = "foundry_workspace_test"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def docker_client():
    try:
        client = build_docker_client()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"no reachable Docker daemon: {exc}")
    return client


@pytest.fixture(scope="session")
def workspace_image(docker_client):
    try:
        docker_client.images.get(WORKSPACE_IMAGE)
    except docker.errors.ImageNotFound:
        docker_client.images.build(path=DOCKERFILE_DIR, tag=WORKSPACE_IMAGE)
    return WORKSPACE_IMAGE


@pytest.fixture(scope="session")
def workspace_network(docker_client):
    try:
        docker_client.networks.get(TEST_NETWORK_NAME)
    except docker.errors.NotFound:
        docker_client.networks.create(TEST_NETWORK_NAME, driver="bridge")
    return TEST_NETWORK_NAME


@pytest.fixture
def executor_config(tmp_path_factory, workspace_image, workspace_network):
    projects_root = tmp_path_factory.mktemp("projects")
    return ExecutorConfig(
        projects_host_path=projects_root,
        workspace_image=workspace_image,
        network_name=workspace_network,
        command_timeout_seconds=30,
        stacks=load_stacks_config(Path(DEFAULT_STACKS_CONFIG_PATH)),
    )


@pytest.fixture
def project_id(executor_config):
    new_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    (executor_config.projects_host_path / new_project_id).mkdir()
    return new_project_id


@pytest.fixture(scope="session")
def node_workspace_image(docker_client):
    try:
        docker_client.images.get(NODE_WORKSPACE_IMAGE)
    except docker.errors.ImageNotFound:
        docker_client.images.build(path=NODE_DOCKERFILE_DIR, tag=NODE_WORKSPACE_IMAGE)
    return NODE_WORKSPACE_IMAGE


@pytest.fixture
def node_project_id(executor_config, node_workspace_image):
    new_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    project_dir = executor_config.projects_host_path / new_project_id
    project_dir.mkdir()
    (project_dir / "package.json").write_text(
        '{"name": "sandbox-check", "version": "1.0.0",'
        ' "dependencies": {"left-pad": "1.3.0"}}'
    )
    return new_project_id


def test_write_file_round_trip_is_visible_on_the_host(docker_client, executor_config, project_id):
    tool_write_file(docker_client, executor_config, project_id, "hello.txt", "hi from the sandbox")

    host_file = executor_config.projects_host_path / project_id / "hello.txt"
    assert host_file.read_text() == "hi from the sandbox"


def test_read_file_returns_content_written_on_the_host(
    docker_client, executor_config, project_id
):
    host_file = executor_config.projects_host_path / project_id / "existing.txt"
    host_file.write_text("already here")

    content = tool_read_file(docker_client, executor_config, project_id, "existing.txt")

    assert content == "already here"


def test_run_command_cannot_read_a_sibling_projects_files(
    docker_client, executor_config, project_id
):
    other_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    other_dir = executor_config.projects_host_path / other_project_id
    other_dir.mkdir()
    (other_dir / "secret.txt").write_text("sibling project secret")

    result = tool_run_command(
        docker_client,
        executor_config,
        project_id,
        ["cat", f"/workspace/../{other_project_id}/secret.txt"],
    )

    assert "sibling project secret" not in result.stdout
    assert result.exit_code != 0


def test_run_command_cannot_reach_the_docker_socket(docker_client, executor_config, project_id):
    result = tool_run_command(
        docker_client, executor_config, project_id, ["test", "-e", "/var/run/docker.sock"]
    )

    assert result.exit_code != 0


def test_run_command_is_capped_by_the_configured_memory_limit(
    docker_client, executor_config, project_id
):
    result = tool_run_command(
        docker_client,
        executor_config,
        project_id,
        ["python3", "-c", "bytearray(4 * 1024 * 1024 * 1024)"],
    )

    assert result.exit_code != 0


def test_node_stack_run_command_installs_dependencies_from_package_json(
    docker_client, executor_config, node_project_id
):
    result = tool_run_command(
        docker_client,
        executor_config,
        node_project_id,
        ["node", "-e", "console.log(require('left-pad')('7', 3, '0'))"],
        timeout=120,
        stack="node",
    )

    assert result.exit_code == 0
    assert "007" in result.stdout


def test_command_exceeding_timeout_raises_and_leaves_no_orphaned_container(
    docker_client, executor_config, project_id
):
    with pytest.raises(CommandTimeoutError):
        tool_run_command(docker_client, executor_config, project_id, ["sleep", "30"], timeout=1)

    leftover = docker_client.containers.list(
        all=True, filters={"ancestor": executor_config.workspace_image}
    )
    assert leftover == []
