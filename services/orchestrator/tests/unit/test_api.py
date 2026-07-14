"""Unit tests for the Orchestrator's FastAPI surface."""

import pytest
from fastapi.testclient import TestClient

from services.orchestrator.api import build_app
from services.orchestrator.executor_client import CommandResult, TestResult
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.tests.unit.planner_fakes import (
    fast_track_router,
    fast_track_router_with_curation,
)
from services.orchestrator.memory.global_store import build_global_memory_connection
from services.orchestrator.progress_buffer import add_progress, get_chunks_since
from services.orchestrator.service import OrchestratorService


class _StubExecutorClient:
    """Reports a clean passing test run and successful git commands for
    tests that drive the graph all the way to node_testing/node_done."""

    async def run_tests(
        self, project_id: str, framework: str = "pytest", stack: str = "python"
    ) -> TestResult:
        return TestResult(passed=1, failed=0, coverage_pct=100.0, report="1 passed")

    async def run_command(
        self, project_id: str, command, timeout=None, stack: str = "python"
    ) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)


@pytest.fixture
def client(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        fast_track_router_with_curation(rounds=2),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
    )
    return TestClient(build_app(service))


def test_webhook_creates_a_new_project_and_pauses_at_plan_approval(client):
    response = client.post("/webhook", json={"user_id": 1, "text": "build me a thing"})

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"]
    assert body["phase"] == "awaiting_plan_approval"


def test_webhook_with_project_id_is_idempotent(client):
    first = client.post(
        "/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"}
    ).json()
    second = client.post(
        "/webhook", json={"user_id": 1, "text": "hi again", "project_id": "my-project"}
    ).json()

    assert first == second


def test_resume_unknown_thread_returns_404(client):
    response = client.post("/resume/does-not-exist", json={"decision": "approve"})

    assert response.status_code == 404


def test_resume_known_thread_progresses_through_merge_gate_to_done(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    after_plan_approval = client.post("/resume/my-project", json={"decision": "approve"})
    assert after_plan_approval.json()["phase"] == "awaiting_merge_approval"

    after_merge_approval = client.post("/resume/my-project", json={"decision": "approve"})
    assert after_merge_approval.status_code == 200
    assert after_merge_approval.json()["phase"] == "done"


def test_status_unknown_project_returns_404(client):
    response = client.get("/status/does-not-exist")

    assert response.status_code == 404


def test_status_known_project_returns_current_phase(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.get("/status/my-project")

    assert response.status_code == 200
    assert response.json()["phase"] == "awaiting_plan_approval"


def test_status_includes_task_progress_once_tasks_exist(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})
    client.post("/resume/my-project", json={"decision": "approve"})

    response = client.get("/status/my-project")

    body = response.json()
    assert body["task_progress"] == {
        "done": 1,
        "total": 1,
        "tasks": [{"description": "Do the trivial thing", "status": "done", "attempts": 0}],
    }


def test_list_projects_returns_created_ids(tmp_path):
    """Uses fast_track_router() (no curator responses), not the shared
    `client` fixture: neither project here ever resumes, so curator_source
    is never set and the Memory Curator never fires -- inserting curator
    responses into the shared queue would misalign project-b's consumption,
    which starts immediately after project-a's 3 items with no resume in
    between to "absorb" a curator slot."""
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer, fast_track_router(rounds=2), _StubExecutorClient(), memory_conn, tmp_path / "projects"
    )
    client = TestClient(build_app(service))

    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "project-a"})
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "project-b"})

    response = client.get("/projects")

    assert response.status_code == 200
    assert response.json()["project_ids"] == ["project-a", "project-b"]


def test_stream_returns_chunks_recorded_after_since_seq(client):
    add_progress("stream-project", "hello ")
    add_progress("stream-project", "world")

    response = client.get("/stream/stream-project", params={"since_seq": 0})

    assert response.status_code == 200
    assert response.json()["chunks"] == [{"seq": 1, "text": "world"}]


def test_stream_unknown_project_returns_an_empty_chunk_list(client):
    response = client.get("/stream/no-such-project")

    assert response.status_code == 200
    assert response.json()["chunks"] == []


def test_status_of_a_terminal_project_clears_its_progress_buffer(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "terminal-project"})
    add_progress("terminal-project", "some progress")

    client.post("/resume/terminal-project", json={"decision": "approve"})
    client.post("/resume/terminal-project", json={"decision": "approve"})

    assert get_chunks_since("terminal-project") == []
