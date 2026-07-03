"""Real-Docker + real-Claude-vision end-to-end test for the Frontend Reviewer
agent (TODO.md Phase 12 Definition of Done: "an end-to-end test captures a
real screenshot of a fixture app and gets a real vision-model verdict back").

Combines the two patterns already established in this test tier:
- test_coder_end_to_end.py's real Executor FastAPI app backed by a real
  Docker client (in-process ASGI, no open port needed), proving
  take_screenshot actually works against a live workspace container --
  this is also the test that would have caught tool_take_screenshot's
  original implementation never actually rendering a page at all (it
  fetched the URL's raw bytes and handed them to PIL.Image.open(), which
  can't decode HTML), plus the missing auto_start_server=True (both fixed
  in this validation pass -- see ADR-024 for the Playwright rewrite).
- test_reviewer_reliability.py's explicit opt-in env var gate before
  spending real, billed Claude API quota.

Unlike test_reviewer_reliability.py, this does not assert a 5-repeated-runs
reliability bar -- .claude/rules/testing.md's "run 5 times, >= 4/5" rule is
scoped to the Reviewer and Tester agents specifically, and Phase 12's own
Definition of Done only asks for one real screenshot -> one real verdict,
not a statistical claim about how often the model catches the fixture's
defect.

Run with:
    FOUNDRY_RUN_FRONTEND_REVIEWER_E2E_TEST=1 pytest -m integration \\
        services/orchestrator/agents/tests/integration/test_frontend_reviewer_e2e.py
"""

import asyncio
import os
from pathlib import Path

import docker
import httpx
import pytest

from services.executor.api import build_app
from services.executor.config import ExecutorConfig
from services.executor.docker_client import build_docker_client
from services.orchestrator.agents.frontend_reviewer import review_frontend
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.model_router.config import load_models_config
from services.orchestrator.model_router.router import ModelRouter

WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DOCKERFILE_DIR = "images/workspace-python"
TEST_NETWORK_NAME = "foundry_workspace_test"
SERVER_URL = "http://localhost:8000"

pytestmark = pytest.mark.integration

RUN_ENV_VAR = "FOUNDRY_RUN_FRONTEND_REVIEWER_E2E_TEST"

_FIXTURE_INDEX_HTML = """<!doctype html>
<html>
<head><title>Fixture App</title></head>
<body style="background:#ffffff; color:#ffffff; font-family:sans-serif;">
  <h1>Welcome</h1>
  <p>This text is white on a white background and should be unreadable.</p>
  <button style="position:absolute; top:10px; left:10px;">Click me</button>
  <button style="position:absolute; top:14px; left:12px;">Also click me</button>
</body>
</html>
"""


@pytest.fixture(scope="module")
def anthropic_api_key() -> str:
    """Resolves a Claude API key, but only if this test was explicitly opted into.

    See test_reviewer_reliability.py's identical fixture docstring for why
    credential presence alone isn't sufficient gating.
    """
    if os.environ.get(RUN_ENV_VAR) != "1":
        pytest.skip(
            f"set {RUN_ENV_VAR}=1 to run this test -- it makes a real, billed Claude API call"
        )
    secret_path = Path("/run/secrets/claude_api_key")
    if secret_path.exists():
        return secret_path.read_text().strip()
    for env_var in ("CLAUDE_API_KEY", "ANTHROPIC_API_KEY"):
        value = os.environ.get(env_var)
        if value:
            return value
    pytest.skip(
        "no Claude API key available (/run/secrets/claude_api_key or "
        "CLAUDE_API_KEY/ANTHROPIC_API_KEY)"
    )


@pytest.fixture(scope="module")
def router(anthropic_api_key) -> ModelRouter:
    config = load_models_config(Path("config/models.claude-only.yaml"))
    return ModelRouter(config, httpx.AsyncClient(), anthropic_api_key=anthropic_api_key)


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
        command_timeout_seconds=60,
    )
    app = build_app(docker_client, config)
    transport = httpx.ASGITransport(app=app)
    return ExecutorClient("http://test-executor", httpx.AsyncClient(transport=transport))


@pytest.fixture
def project_id(projects_root):
    new_project_id = "proj-frontend-e2e"
    project_dir = projects_root / new_project_id
    project_dir.mkdir()
    (project_dir / "index.html").write_text(_FIXTURE_INDEX_HTML)
    return new_project_id


def test_frontend_reviewer_captures_a_real_screenshot_and_gets_a_real_vision_verdict(
    router, executor_client, project_id
):
    verdict, findings = asyncio.run(
        review_frontend(router, executor_client, project_id, url=SERVER_URL)
    )

    assert verdict in ("approve", "changes-requested")
    if verdict == "changes-requested":
        assert findings
        assert all(f["severity"] in ("critical", "warning") for f in findings)
