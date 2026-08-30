"""Unit tests for the Orchestrator's FastAPI surface."""

import pytest
from fastapi.testclient import TestClient

from services.orchestrator.api import build_app
from services.orchestrator.executor_client import CommandResult, FileEntry, TestResult
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.tests.unit.planner_fakes import (
    fast_track_router_with_curation,
    start_only_router,
)
from services.orchestrator.memory.global_store import build_global_memory_connection
from services.orchestrator.progress_buffer import add_progress, get_chunks_since
from services.orchestrator.service import OrchestratorService
from services.orchestrator.stacks import StacksConfig


class _StubExecutorClient:
    """Reports a clean passing test run and successful git commands for
    tests that drive the graph all the way to node_testing/node_done.

    list_files/read_file report one dummy file so node_reviewer's
    review_changes() has something to send to the (fake) LLM instead of
    failing closed (see agents/reviewer.py) -- fast_track_router()'s
    CODER_DONE has no tool_calls, so changed_files stays empty and
    review_changes() always falls back to this list_files/read_file path."""

    async def run_tests(
        self, project_id: str, framework: str = "pytest", stack: str = "python"
    ) -> TestResult:
        return TestResult(passed=1, failed=0, coverage_pct=100.0, report="1 passed")

    async def run_command(
        self, project_id: str, command, timeout=None, stack: str = "python"
    ) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)

    async def list_files(
        self, project_id: str, path: str = ".", stack: str = "python"
    ) -> list[FileEntry]:
        return [FileEntry(name="main.py", is_dir=False)]

    async def read_file(self, project_id: str, path: str, stack: str = "python") -> str:
        return "x = 1\n"


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
        StacksConfig(stacks={}),
    )
    return TestClient(build_app(service))


def test_health_returns_ok_without_touching_the_service(client):
    """docker-compose.yml's healthcheck (and the Telegram Gateway's
    depends_on: condition: service_healthy gate) needs this to succeed the
    moment the FastAPI server is accepting connections, before any graph or
    memory call is ever made."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_webhook_creates_a_new_project_and_pauses_at_plan_approval(client):
    response = client.post("/webhook", json={"user_id": 1, "text": "build me a thing"})

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"]
    assert body["phase"] == "awaiting_plan_approval"


def test_webhook_rejects_a_path_traversal_project_id_with_400(client):
    response = client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "../../etc"})

    assert response.status_code == 400


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
    """Uses start_only_router(), not the shared `client` fixture: neither
    project here ever resumes past plan_wait, so each project's only
    invoke() call consumes just the Planner's 3-item interview->draft->
    critique sequence -- fast_track_router()'s 6-item per-round grouping
    (which bundles in Coder/Reviewer responses meant for a resume that
    never happens here) would misalign project-b's consumption onto
    responses shaped for a different call entirely."""
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        start_only_router(projects=2),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    client = TestClient(build_app(service))

    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "project-a"})
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "project-b"})

    response = client.get("/projects")

    assert response.status_code == 200
    assert response.json()["projects"] == [
        {"id": "project-a", "name": None},
        {"id": "project-b", "name": None},
    ]


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


def test_webhook_force_new_starts_a_fresh_project_even_with_an_active_one(tmp_path):
    """Uses start_only_router(), not the shared `client` fixture: neither
    project here ever resumes past plan_wait (see
    test_list_projects_returns_created_ids's identical reasoning above)."""
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        start_only_router(projects=2),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    client = TestClient(build_app(service))

    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.post(
        "/webhook", json={"user_id": 1, "text": "brand new idea", "force_new": True}
    )

    assert response.json()["project_id"] != "my-project"


def test_webhook_response_defaults_text_applied_to_true(client):
    response = client.post(
        "/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"}
    )

    assert response.json()["text_applied"] is True


def test_activate_unknown_project_returns_404(client):
    response = client.post("/activate/does-not-exist")

    assert response.status_code == 404


def test_activate_known_project_returns_its_status(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.post("/activate/my-project")

    assert response.status_code == 200
    assert response.json()["project_id"] == "my-project"


def test_webhook_new_project_response_includes_derived_name(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        start_only_router(projects=1),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    client = TestClient(build_app(service))

    response = client.post("/webhook", json={"user_id": 1, "text": "/new Build me a CLI"})

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == "build-me-a-cli"
    assert body["name"] == "Build Me A Cli"


def test_status_returns_none_name_for_an_unregistered_project(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "my-project"})

    response = client.get("/status/my-project")

    assert response.json()["name"] is None


def test_rename_renames_the_active_project(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        start_only_router(projects=1),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    client = TestClient(build_app(service))
    client.post("/webhook", json={"user_id": 1, "text": "/new Build me a CLI"})

    response = client.post("/rename", json={"name": "CSV Converter"})

    assert response.status_code == 200
    assert response.json()["name"] == "CSV Converter"


def test_rename_with_no_active_project_returns_404(client):
    response = client.post("/rename", json={"name": "New Name"})

    assert response.status_code == 404


def test_rename_with_a_taken_name_returns_409(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        start_only_router(projects=2),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    client = TestClient(build_app(service))
    client.post("/webhook", json={"user_id": 1, "text": "/new First idea", "force_new": True})
    client.post("/webhook", json={"user_id": 1, "text": "/new Second idea", "force_new": True})

    response = client.post("/rename", json={"name": "First Idea"})

    assert response.status_code == 409
