"""Unit tests for OrchestratorService, the global routing layer."""

import logging

import pytest

from services.orchestrator.agents.coder import CoderTask, _TaskList
from services.orchestrator.agents.planner import InterviewTurn
from services.orchestrator.executor_client import CommandResult, FileEntry, TestResult
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.tests.unit.planner_fakes import (
    CODER_DONE,
    CURATION_OK,
    EMPTY_DRAFT,
    NO_FINDINGS_CRITIQUE,
    ONE_TASK,
    READY_TURN,
    REVIEWER_APPROVE,
    FakeRouter,
    fast_track_router_with_curation,
    start_only_router,
    structured,
)
from services.orchestrator.memory.global_store import build_global_memory_connection
from services.orchestrator.progress_buffer import get_chunks_since
from services.orchestrator.registry.errors import DuplicateNameError
from services.orchestrator.registry.store import load_registry
from services.orchestrator.service import OrchestratorService, UnknownProjectError
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
def service(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    return OrchestratorService(
        checkpointer,
        fast_track_router_with_curation(rounds=2),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )


def test_handle_webhook_without_project_id_starts_a_new_project(service):
    result = service.handle_webhook(None, "build me a thing")

    assert result["project_id"]
    assert result["phase"] == "awaiting_plan_approval"


def test_handle_webhook_with_explicit_project_id_starts_that_project(service):
    result = service.handle_webhook("my-project", "build me a thing")

    assert result["project_id"] == "my-project"
    assert result["phase"] == "awaiting_plan_approval"


def test_handle_webhook_rejects_a_path_traversal_project_id(service):
    from services.orchestrator.registry.errors import InvalidProjectIdError

    with pytest.raises(InvalidProjectIdError):
        service.handle_webhook("../../etc", "build me a thing")


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


def test_resume_crash_logs_with_project_context_and_records_a_progress_event(tmp_path, caplog):
    """A node raising mid-run (simulated here the same way
    test_activate_resumes_execution_when_stuck_mid_task_with_no_pending_approval
    does -- an under-scripted FakeRouter running out of responses) must
    leave a project-tagged ERROR log line and a "crashed" progress event
    behind, not vanish silently. The exception itself must still propagate
    unchanged, since callers' existing 500-mapping depends on it."""
    db_path = tmp_path / "foundry_memory.db"
    checkpointer = build_sqlite_checkpointer(db_path)
    memory_conn = build_global_memory_connection(db_path)
    router = FakeRouter([READY_TURN, EMPTY_DRAFT, NO_FINDINGS_CRITIQUE, CURATION_OK])
    service = OrchestratorService(
        checkpointer,
        router,
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("proj-crash", "build me a thing")

    with caplog.at_level(logging.ERROR):
        with pytest.raises(IndexError):
            service.resume("proj-crash", "approve")

    assert any(
        "proj-crash" in record.message and "graph run crashed" in record.message
        for record in caplog.records
    )
    chunks = [chunk.text for chunk in get_chunks_since("proj-crash")]
    assert any(chunk.startswith("❌ crashed:") for chunk in chunks)


def test_status_unknown_project_raises(service):
    with pytest.raises(UnknownProjectError):
        service.status("does-not-exist")


def test_status_before_decomposition_has_no_task_progress(service):
    result = service.handle_webhook("my-project", "hello")

    assert result["phase"] == "awaiting_plan_approval"
    assert result["task_progress"] is None


def test_status_after_plan_approval_includes_task_progress(service):
    service.handle_webhook("my-project", "hello")

    result = service.resume("my-project", "approve")

    assert result["phase"] == "awaiting_merge_approval"
    assert result["task_progress"] == {
        "done": 1,
        "total": 1,
        "tasks": [{"description": "Do the trivial thing", "status": "done", "attempts": 0}],
    }


def test_list_projects_returns_known_ids(tmp_path):
    """Uses start_only_router(), not the shared `service` fixture: neither
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

    service.handle_webhook("project-a", "hello")
    service.handle_webhook("project-b", "hello")

    assert service.list_projects() == [
        {"id": "project-a", "name": None},
        {"id": "project-b", "name": None},
    ]


def test_handle_webhook_resumes_a_plan_questions_paused_active_project_with_free_text(
    tmp_path,
):
    """The core fix: a project paused mid-interview (pending_approval["kind"]
    == "plan_questions") must have free text routed to Command(resume=text),
    not treated as a new project's initial message.

    Response order: (1) InterviewTurn(status="questions") -- node_planner_ask's
    first call, pauses at planner_wait; (2) READY_TURN -- node_planner_ask's
    second call after resume, signals interview complete; (3) EMPTY_DRAFT --
    node_planner_draft; (4) NO_FINDINGS_CRITIQUE -- node_planner_critique,
    pauses at plan_wait. Exactly these 4, in this order, per one project that
    never reaches plan_wait's own resume in this test.
    """
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    responses = [
        structured(InterviewTurn(status="questions", questions=["Which language?"])),
        READY_TURN,
        EMPTY_DRAFT,
        NO_FINDINGS_CRITIQUE,
    ]
    service = OrchestratorService(
        checkpointer,
        FakeRouter(responses),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )

    started = service.handle_webhook(None, "build me a thing", force_new=True)
    assert started["pending_approval"]["kind"] == "plan_questions"

    resumed = service.handle_webhook(None, "Python please")

    assert resumed["project_id"] == started["project_id"]
    assert resumed["phase"] == "awaiting_plan_approval"
    assert "text_applied" not in resumed


def test_handle_webhook_does_not_touch_the_graph_when_active_project_is_mid_execution(
    tmp_path,
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        fast_track_router_with_curation(rounds=1),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("my-project", "hello")
    service.resume("my-project", "approve")

    result = service.handle_webhook(None, "some unrelated text")

    assert result["project_id"] == "my-project"
    assert result["phase"] == "awaiting_merge_approval"
    assert result["text_applied"] is False


def test_handle_webhook_force_new_starts_a_fresh_project_even_with_an_active_one(tmp_path):
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
    service.handle_webhook("my-project", "hello")

    result = service.handle_webhook(None, "brand new idea", force_new=True)

    assert result["project_id"] != "my-project"
    assert result["phase"] == "awaiting_plan_approval"


def test_handle_webhook_starts_fresh_when_active_project_is_terminal(tmp_path):
    """Response order: 9 items driving "my-project" all the way to done
    (READY_TURN/EMPTY_DRAFT/NO_FINDINGS_CRITIQUE/CURATION_OK for the plan
    interview+curator checkpoint, ONE_TASK/CODER_DONE/REVIEWER_APPROVE for
    the one task, CURATION_OK x2 for the testing and merge_wait curator
    checkpoints -- mirrors fast_track_router_with_curation(rounds=1) exactly),
    then 3 more (READY_TURN/EMPTY_DRAFT/NO_FINDINGS_CRITIQUE) for the second
    project's interview, which must NOT reuse "my-project"'s already-consumed
    responses.
    """
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    responses = [
        READY_TURN,
        EMPTY_DRAFT,
        NO_FINDINGS_CRITIQUE,
        CURATION_OK,
        ONE_TASK,
        CODER_DONE,
        REVIEWER_APPROVE,
        CURATION_OK,
        CURATION_OK,
        READY_TURN,
        EMPTY_DRAFT,
        NO_FINDINGS_CRITIQUE,
    ]
    service = OrchestratorService(
        checkpointer,
        FakeRouter(responses),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("my-project", "hello")
    service.resume("my-project", "approve")
    service.resume("my-project", "approve")
    assert service.status("my-project")["phase"] == "done"

    result = service.handle_webhook(None, "another idea")

    assert result["project_id"] != "my-project"
    assert result["phase"] == "awaiting_plan_approval"


def test_handle_webhook_starts_fresh_on_first_ever_message_with_no_active_project(tmp_path):
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

    result = service.handle_webhook(None, "build me a thing")

    assert result["project_id"]
    assert result["phase"] == "awaiting_plan_approval"


def test_activate_marks_a_project_active_for_subsequent_free_text(tmp_path):
    """Response order: two independent projects' interview sequences
    (start_only_router(projects=2)'s own documented shape) -- project-a is
    created first (becoming active by that side effect), project-b second;
    explicitly re-activating project-a must make a later bare free-text
    webhook target project-a again, not project-b."""
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
    service.handle_webhook("project-a", "hello")
    service.handle_webhook("project-b", "hello")

    service.activate("project-a")
    result = service.handle_webhook(None, "some text")

    assert result["project_id"] == "project-a"
    assert result["text_applied"] is False


def test_activate_resumes_execution_when_stuck_mid_task_with_no_pending_approval(tmp_path):
    """Simulates the Orchestrator process dying mid-task-loop (e.g. a Docker
    stack restart): the checkpointer persists state after every completed
    superstep, but nothing re-invokes the compiled graph after a crash on
    its own. A fresh service built from the same on-disk checkpoint (a new
    OrchestratorService/checkpointer pair against the same sqlite file,
    exactly like restarting the process for real) must have activate() pick
    task execution back up, not just write the active-project pointer.

    The "crash" itself is simulated by under-scripting the FakeRouter so its
    FakeChatModel runs out of responses mid-task-two and raises IndexError --
    LangGraph still checkpoints task one's completion first, since that
    superstep finished before the crash.
    """
    db_path = tmp_path / "foundry_memory.db"
    two_tasks = structured(
        _TaskList(tasks=[CoderTask(description="Task 1"), CoderTask(description="Task 2")])
    )
    checkpointer = build_sqlite_checkpointer(db_path)
    memory_conn = build_global_memory_connection(db_path)
    router = FakeRouter(
        [READY_TURN, EMPTY_DRAFT, NO_FINDINGS_CRITIQUE, CURATION_OK, two_tasks, CODER_DONE]
    )
    service = OrchestratorService(
        checkpointer,
        router,
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("proj-1", "build me a thing")

    with pytest.raises(IndexError):
        service.resume("proj-1", "approve")

    stuck_status = service.status("proj-1")
    assert stuck_status["phase"] == "implementing"
    assert stuck_status["pending_approval"] is None
    assert stuck_status["task_progress"]["done"] == 1

    resumed_service = OrchestratorService(
        build_sqlite_checkpointer(db_path),
        FakeRouter([CODER_DONE]),
        _StubExecutorClient(),
        build_global_memory_connection(db_path),
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )

    with pytest.raises(IndexError):
        resumed_service.activate("proj-1")

    final_status = resumed_service.status("proj-1")
    assert final_status["task_progress"]["done"] == 2


def test_activate_does_not_leave_a_stale_crash_marker_for_a_fresh_poller_to_replay(tmp_path):
    """progress_buffer is only otherwise cleared on a terminal-phase status()
    read, so a crash marker from a failed attempt sits there indefinitely. A
    Gateway restarted alongside the Orchestrator resets its poller's
    since_seq to -1 and replays the whole buffer -- which would show
    "❌ crashed: ..." from the *previous* attempt immediately ahead of the
    recovered run's real progress. activate() clears the buffer before
    continuing, so what a fresh poller sees describes only this attempt.

    Uses the same under-scripted-FakeRouter crash simulation as
    test_activate_resumes_execution_when_stuck_mid_task_with_no_pending_approval,
    but scripts the continuation fully (task two's CODER_DONE, the reviewer,
    the post-testing curator) so activate() reaches the merge gate and
    returns normally instead of crashing again.
    """
    db_path = tmp_path / "foundry_memory.db"
    two_tasks = structured(
        _TaskList(tasks=[CoderTask(description="Task 1"), CoderTask(description="Task 2")])
    )
    service = OrchestratorService(
        build_sqlite_checkpointer(db_path),
        FakeRouter(
            [READY_TURN, EMPTY_DRAFT, NO_FINDINGS_CRITIQUE, CURATION_OK, two_tasks, CODER_DONE]
        ),
        _StubExecutorClient(),
        build_global_memory_connection(db_path),
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    service.handle_webhook("proj-stale-crash", "build me a thing")

    with pytest.raises(IndexError):
        service.resume("proj-stale-crash", "approve")

    crashed_chunks = [chunk.text for chunk in get_chunks_since("proj-stale-crash")]
    assert any(chunk.startswith("❌ crashed:") for chunk in crashed_chunks)

    resumed_service = OrchestratorService(
        build_sqlite_checkpointer(db_path),
        FakeRouter([CODER_DONE, REVIEWER_APPROVE, CURATION_OK]),
        _StubExecutorClient(),
        build_global_memory_connection(db_path),
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )
    resumed_service.activate("proj-stale-crash")

    assert resumed_service.status("proj-stale-crash")["task_progress"]["done"] == 2
    replayed = [chunk.text for chunk in get_chunks_since("proj-stale-crash")]
    assert not any(chunk.startswith("❌ crashed:") for chunk in replayed)
    assert replayed


def test_activate_leaves_a_project_paused_at_a_real_gate_untouched(tmp_path):
    """A project paused at plan_approval (pending_approval is not None) must
    not have the graph re-invoked just because it was reactivated -- that
    needs an explicit decision via resume(), not a bare continuation."""
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
    service.handle_webhook("proj-1", "build me a thing")

    result = service.activate("proj-1")

    assert result is None
    status = service.status("proj-1")
    assert status["phase"] == "awaiting_plan_approval"
    assert status["pending_approval"] is not None


def test_handle_webhook_new_project_derives_name_and_slug_id_and_registers_it(tmp_path):
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

    result = service.handle_webhook(
        None, "/new Build me a CLI that converts CSV to JSON", force_new=True
    )

    assert result["project_id"] == "build-me-a-cli-that-converts"
    assert result["name"] == "Build Me A Cli That Converts"
    records = load_registry(tmp_path / "projects" / "projects.yaml")
    assert [r.id for r in records] == ["build-me-a-cli-that-converts"]
    assert records[0].name == "Build Me A Cli That Converts"
    assert records[0].mode == "greenfield"


def test_handle_webhook_new_project_dedupes_name_and_id_on_repeat_message(tmp_path):
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

    first = service.handle_webhook(None, "/new Build me a CLI", force_new=True)
    second = service.handle_webhook(None, "/new Build me a CLI", force_new=True)

    assert first["project_id"] == "build-me-a-cli"
    assert first["name"] == "Build Me A Cli"
    assert second["project_id"] == "build-me-a-cli-2"
    assert second["name"] == "Build Me A Cli (2)"


def test_status_resolves_a_registered_project_by_name(tmp_path):
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
    started = service.handle_webhook(None, "/new Build me a CLI", force_new=True)

    result = service.status("build me a cli")

    assert result["project_id"] == started["project_id"]
    assert result["name"] == "Build Me A Cli"


def test_status_unregistered_project_has_no_name(service):
    service.handle_webhook("my-project", "hello")

    result = service.status("my-project")

    assert result["project_id"] == "my-project"
    assert result["name"] is None


def test_rename_active_project_updates_the_registry_and_status(tmp_path):
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
    started = service.handle_webhook(None, "/new Build me a CLI", force_new=True)

    result = service.rename_active_project("CSV Converter")

    assert result["project_id"] == started["project_id"]
    assert result["name"] == "CSV Converter"
    assert service.status(started["project_id"])["name"] == "CSV Converter"


def test_rename_active_project_rejects_a_name_already_in_use(tmp_path):
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
    service.handle_webhook(None, "/new First idea", force_new=True)
    service.handle_webhook(None, "/new Second idea", force_new=True)

    with pytest.raises(DuplicateNameError):
        service.rename_active_project("First Idea")


def test_rename_active_project_raises_when_nothing_is_active(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        FakeRouter([]),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )

    with pytest.raises(UnknownProjectError):
        service.rename_active_project("New Name")


def test_rename_active_project_adopts_a_project_with_no_registry_entry(service):
    service.handle_webhook("my-project", "hello")

    result = service.rename_active_project("My Renamed Project")

    assert result["project_id"] == "my-project"
    assert result["name"] == "My Renamed Project"


def test_activate_unknown_project_raises(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    memory_conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    service = OrchestratorService(
        checkpointer,
        FakeRouter([]),
        _StubExecutorClient(),
        memory_conn,
        tmp_path / "projects",
        StacksConfig(stacks={}),
    )

    with pytest.raises(UnknownProjectError):
        service.activate("does-not-exist")
