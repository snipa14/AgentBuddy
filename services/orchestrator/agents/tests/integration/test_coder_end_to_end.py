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
from langchain_core.messages import AIMessage

from services.executor.api import build_app
from services.executor.config import ExecutorConfig
from services.executor.docker_client import build_docker_client
from services.orchestrator.agents.coder import run_coder_task
from services.orchestrator.executor_client import ExecutorClient

WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DOCKERFILE_DIR = "images/workspace-python"
TEST_NETWORK_NAME = "foundry_workspace_test"

pytestmark = pytest.mark.integration


class _ScriptedCoderModel:
    """Scripts a fixed sequence of tool-calling turns -- no real LLM involved.

    Stands in for the BaseChatModel `ModelRouter.resolve()` would normally
    return: `.bind_tools()` is a no-op (returns self), and `.ainvoke()`
    ignores the messages and returns the next scripted AIMessage.
    """

    def __init__(self, turns: list[AIMessage]):
        self._turns = list(turns)

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        return self._turns.pop(0)


class _ScriptedCoderRouter:
    """Stands in for ModelRouter: `.resolve(role)` returns a scripted model."""

    def __init__(self, turns: list[AIMessage]):
        self._model = _ScriptedCoderModel(turns)

    def resolve(self, role):
        return self._model

    def cost_usd(self, role, usage):
        return 0.0


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
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "hello.txt", "content": "hello from the coder"},
                        "id": "call_1",
                    }
                ],
            ),
            AIMessage(content="done", tool_calls=[]),
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
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {"path": "notes.txt"}, "id": "call_1"}],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "notes.txt", "content": "updated content"},
                        "id": "call_2",
                    }
                ],
            ),
            AIMessage(content="done", tool_calls=[]),
        ]
    )

    result = asyncio.run(
        run_coder_task(router, executor_client, project_id, "update notes.txt")
    )

    assert result.success is True
    assert "original content" in result.transcript[0]
    assert host_file.read_text() == "updated content"
