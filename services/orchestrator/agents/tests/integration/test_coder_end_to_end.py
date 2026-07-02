"""Real-Docker end-to-end test for the Coder agent (TODO.md Phase 9 Definition of Done).

Builds a real Executor FastAPI app (services.executor.api.build_app) backed
by a real Docker client, reached via httpx.ASGITransport (in-process ASGI,
no open port needed) wrapped in a real ExecutorClient -- proving the whole
Orchestrator -> ExecutorClient -> Executor HTTP API -> Docker -> workspace
container -> host-visible file chain actually works, not just the agent's
parsing/looping logic (already covered by tests/unit/test_coder.py's
RecordingExecutorClient, which never touches Docker).

Requires a real, reachable Docker daemon -- see
services/executor/tests/integration/test_executor_sandbox.py for why these
tests use docker.from_env() directly rather than routing through
docker-socket-proxy (a production concern for the Executor's own runtime
wiring, not something a local test run needs).

Run with: pytest -m integration services/orchestrator/agents/tests/integration
"""

import asyncio

import docker
import httpx
import pytest

from services.executor.api import build_app
from services.executor.config import ExecutorConfig
from services.executor.docker_client import build_docker_client
from services.orchestrator.agents.coder import run_coder_task
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.model_router.schemas import CompletionResult

WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DOCKERFILE_DIR = "images/workspace-python"
TEST_NETWORK_NAME = "foundry_workspace_test"

pytestmark = pytest.mark.integration


class _ScriptedCoderRouter:
    """Scripts a fixed sequence of Coder actions -- no real LLM involved.

    Stands in for ModelRouter: `.resolve(role)` returns self, and `.complete()`
    ignores the request and returns the next scripted action's raw text.
    """

    def __init__(self, actions: list[str]):
        self._actions = list(actions)

    def resolve(self, role):
        return self

    def supports(self, capability):
        return True

    async def complete(self, request):
        return CompletionResult(content=self._actions.pop(0), model="fake", provider_name="fake")


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
def projects_root(tmp_path_factory):
    return tmp_path_factory.mktemp("projects")


@pytest.fixture
def executor_client(docker_client, workspace_image, workspace_network, projects_root):
    config = ExecutorConfig(
        projects_host_path=projects_root,
        workspace_image=workspace_image,
        network_name=workspace_network,
        command_timeout_seconds=30,
    )
    app = build_app(docker_client, config)
    transport = httpx.ASGITransport(app=app)
    return ExecutorClient("http://test-executor", httpx.AsyncClient(transport=transport))


@pytest.fixture
def project_id(projects_root):
    new_project_id = "proj-coder-e2e"
    (projects_root / new_project_id).mkdir()
    return new_project_id


def test_coder_writes_a_real_file_inside_a_real_workspace_container(
    executor_client, projects_root, project_id
):
    router = _ScriptedCoderRouter(
        [
            "## Action\nwrite_file\n\n## Path\nhello.txt\n\n## Content\nhello from the coder\n",
            "## Action\ndone\n",
        ]
    )

    result = asyncio.run(
        run_coder_task(router, executor_client, project_id, "create hello.txt with a greeting")
    )

    assert result.success is True
    host_file = projects_root / project_id / "hello.txt"
    assert host_file.read_text() == "hello from the coder"


def test_coder_reads_an_existing_file_before_overwriting_it(
    executor_client, projects_root, project_id
):
    host_file = projects_root / project_id / "notes.txt"
    host_file.write_text("original content")
    router = _ScriptedCoderRouter(
        [
            "## Action\nread_file\n\n## Path\nnotes.txt\n",
            "## Action\nwrite_file\n\n## Path\nnotes.txt\n\n## Content\nupdated content\n",
            "## Action\ndone\n",
        ]
    )

    result = asyncio.run(
        run_coder_task(router, executor_client, project_id, "update notes.txt")
    )

    assert result.success is True
    assert "original content" in result.transcript[0]
    assert host_file.read_text() == "updated content"
