"""Real-Docker integration test: a Go project round-trips on the generated image.

Phase 22's Definition of Done (ADR-045): dependency install resolves a real
module through the go stack's cache volume and the project builds and runs
inside the template-generated foundry/workspace-go image. Requires a Docker
daemon, the built image, and network egress from the workspace bridge
network; skipped automatically when the daemon or image is absent. Mirrors
test_executor_sandbox.py's session-scoped Docker client/network fixtures and
uuid-suffixed project ids so a bare host without the compose-created
foundry_workspace network still runs this suite by creating its own.
"""

import uuid
from pathlib import Path

import docker
import pytest

from services.executor.config import ExecutorConfig
from services.executor.docker_client import build_docker_client
from services.executor.stacks import load_stacks_config
from services.executor.tools import tool_run_command, tool_run_tests, tool_write_file

pytest.importorskip("yaml")

WORKSPACE_IMAGE = "foundry/workspace-go:1.22"
TEST_NETWORK_NAME = "foundry_workspace_test"

GO_MOD = """module foundrytest

go 1.22

require rsc.io/quote v1.5.2
"""

MAIN_GO = """package main

import (
	"fmt"

	"rsc.io/quote"
)

func Greeting() string {
	return quote.Hello()
}

func main() {
	fmt.Println(Greeting())
}
"""

MAIN_TEST_GO = """package main

import "testing"

func TestGreetingIsNotEmpty(t *testing.T) {
	if Greeting() == "" {
		t.Fatal("empty greeting")
	}
}
"""

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
        pytest.skip(f"{WORKSPACE_IMAGE} not built on this host")
    return WORKSPACE_IMAGE


@pytest.fixture(scope="session")
def workspace_network(docker_client):
    try:
        docker_client.networks.get(TEST_NETWORK_NAME)
    except docker.errors.NotFound:
        docker_client.networks.create(TEST_NETWORK_NAME, driver="bridge")
    return TEST_NETWORK_NAME


@pytest.fixture
def executor_config(tmp_path, workspace_image, workspace_network):
    return ExecutorConfig(
        projects_host_path=tmp_path,
        workspace_image=workspace_image,
        network_name=workspace_network,
        stacks=load_stacks_config(Path("config/stacks.yaml")),
    )


@pytest.fixture
def project_id(executor_config):
    new_project_id = f"go-int-{uuid.uuid4().hex[:8]}"
    (executor_config.projects_host_path / new_project_id).mkdir()
    return new_project_id


def test_go_project_installs_dependency_and_builds(docker_client, executor_config, project_id):
    tool_write_file(docker_client, executor_config, project_id, "go.mod", GO_MOD, stack="go")
    tool_write_file(docker_client, executor_config, project_id, "main.go", MAIN_GO, stack="go")
    tool_write_file(
        docker_client, executor_config, project_id, "main_test.go", MAIN_TEST_GO, stack="go"
    )

    tidy = tool_run_command(
        docker_client, executor_config, project_id, ["go", "mod", "tidy"], stack="go"
    )
    assert tidy.exit_code == 0, tidy.stderr

    build = tool_run_command(
        docker_client, executor_config, project_id, ["go", "build", "./..."], stack="go"
    )
    assert build.exit_code == 0, build.stderr

    run = tool_run_command(
        docker_client, executor_config, project_id, ["go", "run", "."], stack="go"
    )
    assert run.exit_code == 0, run.stderr
    assert "Hello" in run.stdout

    tests = tool_run_tests(docker_client, executor_config, project_id, stack="go")
    assert "ok" in tests.report
    assert "FAIL" not in tests.report
