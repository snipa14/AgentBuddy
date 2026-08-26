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

import os
import shutil
import uuid
from pathlib import Path
from typing import NamedTuple

import docker
import pytest

from services.executor.config import DEFAULT_STACKS_CONFIG_PATH, ExecutorConfig
from services.executor.containers import WORKSPACE_UID, cache_volume_name
from services.executor.docker_client import build_docker_client
from services.executor.errors import CommandTimeoutError
from services.executor.stacks import load_stacks_config
from services.executor.tools import tool_read_file, tool_run_command, tool_write_file

WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DOCKERFILE_DIR = "images/workspace-python"
NODE_WORKSPACE_IMAGE = "foundry/workspace-node:20"
NODE_DOCKERFILE_DIR = "images/workspace-node"
TEST_NETWORK_NAME = "foundry_workspace_test"

REPO_ROOT = Path(__file__).resolve().parents[4]
HOST_WORKSPACE_ROOT = os.environ.get("FOUNDRY_TEST_HOST_WORKSPACE_ROOT")
_DOOD_LOCAL_BASE = REPO_ROOT / ".pytest-dood-projects"


class ProjectRoots(NamedTuple):
    """Where this test process should read/write vs. where Docker should bind-mount from.

    Attributes:
        local: The path this pytest process can open()/mkdir() directly.
        host: The path to hand to ExecutorConfig.projects_host_path -- what
            the Docker daemon actually resolves bind-mount sources against.
    """

    local: Path
    host: Path


def _make_project_roots(tmp_path_factory) -> ProjectRoots:
    """Builds a fresh projects-root pair for one test.

    When FOUNDRY_TEST_HOST_WORKSPACE_ROOT is unset, this test process and
    the Docker daemon share one filesystem (the common case: pytest running
    directly on the same host as the daemon), so an ordinary pytest tmp dir
    works as both paths.

    When it is set, this test process runs inside a container that reaches
    a *host* Docker daemon over a bind-mounted socket (Docker-outside-of-
    Docker -- see SECURITY.md's "DooD detail", which documents the same
    shape of problem for the Executor's own production deployment and
    solves it with PROJECTS_HOST_PATH). FOUNDRY_TEST_HOST_WORKSPACE_ROOT is
    that same idea applied to this test process: the host-side absolute
    path this container's repo checkout is bind-mounted from. A tmp dir is
    created under the repo root (so it's covered by that same bind mount)
    and its host-side equivalent is derived by substituting the repo root
    prefix.

    Args:
        tmp_path_factory: pytest's tmp_path_factory fixture.

    Returns:
        A ProjectRoots pair.
    """
    if not HOST_WORKSPACE_ROOT:
        local = tmp_path_factory.mktemp("projects")
        return ProjectRoots(local=local, host=local)
    local = _DOOD_LOCAL_BASE / uuid.uuid4().hex[:8]
    local.mkdir(parents=True)
    host = Path(HOST_WORKSPACE_ROOT) / local.relative_to(REPO_ROOT)
    return ProjectRoots(local=local, host=host)


def _chown_to_workspace_uid_if_root(path: Path) -> None:
    """Chowns *path* to the workspace container's UID, mirroring production.

    services/orchestrator/registry/registration.py does this for every real
    project directory immediately after creating it (via
    services.orchestrator.memory.project_memory.chown_to_workspace_uid) --
    a directory created by a root process (the Orchestrator's own container
    runs as root) is otherwise left root-owned at mode 755, which blocks
    the non-root UID-1000 workspace container from creating anything inside
    it. This test's own directories need the same treatment to faithfully
    reproduce production's actual permission state; a non-root test process
    can't chown to an arbitrary UID at all, so this is a no-op in that case.

    Args:
        path: The directory to chown.
    """
    if os.geteuid() == 0:
        os.chown(path, WORKSPACE_UID, WORKSPACE_UID)


@pytest.fixture(scope="session", autouse=True)
def _cleanup_dood_local_projects_dir():
    yield
    if HOST_WORKSPACE_ROOT:
        shutil.rmtree(_DOOD_LOCAL_BASE, ignore_errors=True)


@pytest.fixture
def project_roots(tmp_path_factory):
    return _make_project_roots(tmp_path_factory)


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
def executor_config(project_roots, workspace_image, workspace_network):
    return ExecutorConfig(
        projects_host_path=project_roots.host,
        workspace_image=workspace_image,
        network_name=workspace_network,
        command_timeout_seconds=30,
        stacks=load_stacks_config(Path(DEFAULT_STACKS_CONFIG_PATH)),
    )


@pytest.fixture
def project_id(project_roots, docker_client):
    """Mints a fresh project id and removes its stray python cache volume afterward.

    tool_run_command's default stack is python, so any test that runs a
    command through this project id may cause containers.py's Phase-17
    cache-volume mechanism to create a `foundry-cache-{project_id}-python`
    named volume; without this cleanup every such test run strands one more
    volume on the daemon forever. Not every test using this fixture calls
    tool_run_command, so the volume may never have been created -- that's
    tolerated below, not an error.
    """
    new_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    project_dir = project_roots.local / new_project_id
    project_dir.mkdir()
    _chown_to_workspace_uid_if_root(project_dir)
    yield new_project_id
    try:
        docker_client.volumes.get(cache_volume_name(new_project_id, "python")).remove()
    except docker.errors.NotFound:
        pass


@pytest.fixture(scope="session")
def node_workspace_image(docker_client):
    try:
        docker_client.images.get(NODE_WORKSPACE_IMAGE)
    except docker.errors.ImageNotFound:
        docker_client.images.build(path=NODE_DOCKERFILE_DIR, tag=NODE_WORKSPACE_IMAGE)
    return NODE_WORKSPACE_IMAGE


@pytest.fixture
def node_project_id(project_roots, node_workspace_image, docker_client):
    """Mints a fresh node-stack project id and removes its stray cache volume afterward.

    Same rationale as project_id's docstring, but for the node stack's
    `foundry-cache-{project_id}-node` volume.
    """
    new_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    project_dir = project_roots.local / new_project_id
    project_dir.mkdir()
    _chown_to_workspace_uid_if_root(project_dir)
    (project_dir / "package.json").write_text(
        '{"name": "sandbox-check", "version": "1.0.0", "dependencies": {"left-pad": "1.3.0"}}'
    )
    yield new_project_id
    try:
        docker_client.volumes.get(cache_volume_name(new_project_id, "node")).remove()
    except docker.errors.NotFound:
        pass


def test_write_file_round_trip_is_visible_on_the_host(
    docker_client, executor_config, project_roots, project_id
):
    tool_write_file(docker_client, executor_config, project_id, "hello.txt", "hi from the sandbox")

    host_file = project_roots.local / project_id / "hello.txt"
    assert host_file.read_text() == "hi from the sandbox"


def test_read_file_returns_content_written_on_the_host(
    docker_client, executor_config, project_roots, project_id
):
    host_file = project_roots.local / project_id / "existing.txt"
    host_file.write_text("already here")

    content = tool_read_file(docker_client, executor_config, project_id, "existing.txt")

    assert content == "already here"


def test_run_command_cannot_read_a_sibling_projects_files(
    docker_client, executor_config, project_roots, project_id
):
    other_project_id = f"proj-{uuid.uuid4().hex[:8]}"
    other_dir = project_roots.local / other_project_id
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


def test_run_command_fork_bomb_is_capped_and_leaves_no_orphaned_container(
    docker_client, executor_config, project_id
):
    result = tool_run_command(
        docker_client,
        executor_config,
        project_id,
        ["bash", "-c", ":(){ :|:& };:"],
        timeout=15,
    )

    # pids_limit prevents unlimited forking: bash reports EAGAIN/Resource unavailable errors
    # or the command exits non-zero. Both cases prove the fork bomb was capped.
    assert result.exit_code != 0 or "Resource temporarily unavailable" in result.stderr

    leftover = docker_client.containers.list(
        all=True, filters={"ancestor": executor_config.workspace_image}
    )
    assert leftover == []


def test_run_command_never_logs_literal_file_contents(
    docker_client, executor_config, project_roots, project_id, caplog
):
    fake_secret_value = "totally-not-a-real-credential-value-12345"
    secret_file = project_roots.local / project_id / "secret.env"
    secret_file.write_text(f"API_KEY={fake_secret_value}\n")

    with caplog.at_level("DEBUG"):
        tool_run_command(docker_client, executor_config, project_id, ["cat", "secret.env"])

    for record in caplog.records:
        assert fake_secret_value not in record.getMessage()
