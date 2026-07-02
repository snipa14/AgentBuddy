"""Unit tests for the Orchestrator's FastAPI surface."""

import pytest
from fastapi.testclient import TestClient

from services.orchestrator.api import build_app
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.tests.unit.planner_fakes import fast_track_router
from services.orchestrator.service import OrchestratorService


@pytest.fixture
def client(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    unused_executor_client = object()
    service = OrchestratorService(
        checkpointer, fast_track_router(rounds=2), unused_executor_client
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


def test_list_projects_returns_created_ids(client):
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "project-a"})
    client.post("/webhook", json={"user_id": 1, "text": "hi", "project_id": "project-b"})

    response = client.get("/projects")

    assert response.status_code == 200
    assert response.json()["project_ids"] == ["project-a", "project-b"]
