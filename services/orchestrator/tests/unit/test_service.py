"""Unit tests for OrchestratorService, the global routing layer."""

import pytest

from services.orchestrator.executor_client import CommandResult, TestResult
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.tests.unit.planner_fakes import fast_track_router
from services.orchestrator.memory.global_store import build_global_memory_connection
from services.orchestrator.service import OrchestratorService, UnknownProjectError


class _StubExecutorClient:
    """Reports a clean passing test run and successful git commands for
    tests that drive the graph all the way to node_testing/node_done."""

    async def run_tests(self, project_id: str, framework: str = "pytest") -> TestResult:
        return TestResult(passed=1, failed=0, coverage_pct=100.0, report="1 passed")

    async def run_command(self, project_id: str, command, timeout=None) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)


@pytest.fixture
def service(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    return OrchestratorService(
        checkpointer, fast_track_router(rounds=2), _StubExecutorClient(), memory_conn, tmp_path / "projects"
    )


def test_handle_webhook_without_project_id_starts_a_new_project(service):
    result = service.handle_webhook(None, "build me a thing")

    assert result["project_id"]
    assert result["phase"] == "awaiting_plan_approval"


def test_handle_webhook_with_explicit_project_id_starts_that_project(service):
    result = service.handle_webhook("my-project", "build me a thing")

    assert result["project_id"] == "my-project"
    assert result["phase"] == "awaiting_plan_approval"


def test_handle_webhook_is_idempotent_status_echo_for_known_project(service):
    first = service.handle_webhook("my-project", "hello")
    second = service.handle_webhook("my-project", "hello again")

    assert first == second


def test_resume_unknown_project_raises(service):
    with pytest.raises(UnknownProjectError):
        service.resume("does-not-exist", "approve")


def test_resume_known_project_progresses_through_merge_gate_to_done(service):
    service.handle_webhook("my-project", "hello")

    after_plan_approval = service.resume("my-project", "approve")
    assert after_plan_approval["phase"] == "awaiting_merge_approval"

    after_merge_approval = service.resume("my-project", "approve")
    assert after_merge_approval["phase"] == "done"
    assert after_merge_approval["pending_approval"] is None


def test_status_unknown_project_raises(service):
    with pytest.raises(UnknownProjectError):
        service.status("does-not-exist")


def test_list_projects_returns_known_ids(service):
    service.handle_webhook("project-a", "hello")
    service.handle_webhook("project-b", "hello")

    assert service.list_projects() == ["project-a", "project-b"]
