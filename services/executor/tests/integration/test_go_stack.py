"""Real-Docker integration test: a Go project round-trips on the generated image.

Phase 22's Definition of Done (ADR-045): dependency install resolves a real
module through the go stack's cache volume and the project builds and runs
inside the template-generated foundry/workspace-go image. Requires a Docker
daemon, the built image, and network egress from the foundry_workspace
network; skipped automatically when either is absent.
"""

from pathlib import Path

import pytest

from services.executor.config import ExecutorConfig
from services.executor.docker_client import build_docker_client
from services.executor.stacks import load_stacks_config
from services.executor.tools import tool_run_command, tool_run_tests, tool_write_file

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


@pytest.mark.integration
def test_go_project_installs_dependency_and_builds(tmp_path):
	pytest.importorskip("yaml")
	try:
		client = build_docker_client()
		client.ping()
	except Exception as exc:
		pytest.skip(f"no reachable Docker daemon: {exc}")
	try:
		client.images.get("foundry/workspace-go:1.22")
	except Exception:
		pytest.skip("foundry/workspace-go:1.22 not built on this host")

	project_id = "go-int-test"
	(tmp_path / project_id).mkdir()
	config = ExecutorConfig(
		projects_host_path=tmp_path,
		stacks=load_stacks_config(Path("config/stacks.yaml")),
	)
	tool_write_file(client, config, project_id, "go.mod", GO_MOD, stack="go")
	tool_write_file(client, config, project_id, "main.go", MAIN_GO, stack="go")
	tool_write_file(client, config, project_id, "main_test.go", MAIN_TEST_GO, stack="go")

	build = tool_run_command(
		client, config, project_id, ["go", "build", "./..."], stack="go"
	)
	assert build.exit_code == 0, build.stderr

	run = tool_run_command(client, config, project_id, ["go", "run", "."], stack="go")
	assert run.exit_code == 0, run.stderr
	assert "Hello" in run.stdout

	tests = tool_run_tests(client, config, project_id, stack="go")
	assert "ok" in tests.report
	assert "FAIL" not in tests.report
