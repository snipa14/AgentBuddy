"""Unit tests for the Coder agent.

Per .claude/rules/testing.md, LLM-backed agent tests mock the provider at
the model_router.resolve() boundary (router_fakes.py, shared with
test_planner.py/test_reviewer.py). RecordingExecutorClient fakes the
Executor side and records every call made to it, so tests can assert on
*how* the Coder worked (the exact sequence of tool calls), not just the
final file state -- per TODO.md Phase 9's explicit ask.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage

from services.orchestrator.agents.coder import (
    CoderTask,
    _TaskList,
    decompose_plan_into_tasks,
    run_coder_task,
)
from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter, structured, usage
from services.orchestrator.executor_client import CommandResult, ExecutorClientError
from services.orchestrator.progress_buffer import get_chunks_since

TASK_LIST = _TaskList(
    tasks=[
        CoderTask(description="Add a User model with email and password_hash fields"),
        CoderTask(description="Add a POST /login endpoint"),
    ]
)


def _tool_call_message(name: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
        usage_metadata=usage(),
    )


def _done_message() -> AIMessage:
    return AIMessage(content="done", tool_calls=[], usage_metadata=usage())


class RecordingExecutorClient:
    """Fakes ExecutorClient, recording every call made to it in order."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.calls: list[tuple] = []

    async def read_file(self, project_id: str, path: str, stack: str = "python") -> str:
        self.calls.append(("read_file", project_id, path))
        if path not in self.files:
            raise ExecutorClientError(f"404: '{path}' not found")
        return self.files[path]

    async def write_file(
        self, project_id: str, path: str, content: str, stack: str = "python"
    ) -> bool:
        self.calls.append(("write_file", project_id, path, content))
        self.files[path] = content
        return True

    async def run_command(self, project_id, command, timeout=None, stack: str = "python"):
        self.calls.append(("run_command", project_id, tuple(command)))
        return CommandResult(stdout="3 passed", stderr="", exit_code=0)


def test_decompose_plan_into_tasks_returns_task_list():
    router = FakeRouter([structured(TASK_LIST)])

    tasks, message = asyncio.run(
        decompose_plan_into_tasks(router, {"summary": "An auth service", "goals": ["login"]})
    )

    assert [task.description for task in tasks] == [
        "Add a User model with email and password_hash fields",
        "Add a POST /login endpoint",
    ]
    assert message.usage_metadata is not None


def test_decompose_plan_into_tasks_raises_on_no_tasks():
    router = FakeRouter([structured(_TaskList(tasks=[]))])

    with pytest.raises(ValueError, match="no tasks found"):
        asyncio.run(decompose_plan_into_tasks(router, {"summary": "x"}))


def test_run_coder_task_reads_then_writes_then_finishes():
    router = FakeRouter(
        [
            _tool_call_message("read_file", {"path": "models.py"}, "call_1"),
            _tool_call_message(
                "write_file", {"path": "models.py", "content": "class User:\n    pass"}, "call_2"
            ),
            _done_message(),
        ]
    )
    executor = RecordingExecutorClient(files={"models.py": "# empty\n"})

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "Add a User model"))

    assert result.success is True
    assert executor.calls == [
        ("read_file", "proj-1", "models.py"),
        ("write_file", "proj-1", "models.py", "class User:\n    pass"),
    ]
    assert executor.files["models.py"] == "class User:\n    pass"
    assert result.cost_usd > 0


def test_run_coder_task_records_written_file_paths():
    router = FakeRouter(
        [
            _tool_call_message("read_file", {"path": "models.py"}, "call_1"),
            _tool_call_message(
                "write_file", {"path": "models.py", "content": "class User:\n    pass"}, "call_2"
            ),
            _tool_call_message(
                "write_file", {"path": "views.py", "content": "def login(): ..."}, "call_3"
            ),
            _done_message(),
        ]
    )
    executor = RecordingExecutorClient(files={"models.py": "# empty\n"})

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "Add a User model"))

    assert result.written_files == ["models.py", "views.py"]


def test_run_coder_task_written_files_empty_when_no_writes_happen():
    router = FakeRouter([_done_message()])
    executor = RecordingExecutorClient()

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "trivial task"))

    assert result.written_files == []


def test_run_coder_task_records_written_files_even_when_max_steps_exhausted():
    router = FakeRouter(
        [
            _tool_call_message(
                "write_file", {"path": "partial.py", "content": "# wip"}, "call_1"
            )
        ]
        * 3
    )
    executor = RecordingExecutorClient()

    result = asyncio.run(
        run_coder_task(router, executor, "proj-1", "Add a User model", max_steps=3)
    )

    assert result.success is False
    assert result.written_files == ["partial.py", "partial.py", "partial.py"]


def test_run_coder_task_records_a_failed_read_and_still_recovers():
    router = FakeRouter(
        [
            _tool_call_message("read_file", {"path": "models.py"}, "call_1"),
            _tool_call_message(
                "write_file", {"path": "models.py", "content": "class User:\n    pass"}, "call_2"
            ),
            _done_message(),
        ]
    )
    executor = RecordingExecutorClient(files={})

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "Add a User model"))

    assert result.success is True
    assert executor.calls[0] == ("read_file", "proj-1", "models.py")
    assert "error: 404" in result.transcript[0]
    assert executor.calls[1] == ("write_file", "proj-1", "models.py", "class User:\n    pass")


def test_run_coder_task_returns_failure_when_max_steps_exhausted():
    router = FakeRouter([_tool_call_message("read_file", {"path": "models.py"}, "call_1")] * 3)
    executor = RecordingExecutorClient(files={"models.py": "# empty\n"})

    result = asyncio.run(
        run_coder_task(router, executor, "proj-1", "Add a User model", max_steps=3)
    )

    assert result.success is False
    assert len(result.transcript) == 3
    assert len(executor.calls) == 3


def test_run_coder_task_finishes_immediately_when_model_makes_no_tool_calls():
    router = FakeRouter([_done_message()])
    executor = RecordingExecutorClient()

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "trivial task"))

    assert result.success is True
    assert result.transcript == []
    assert executor.calls == []


def test_run_coder_task_includes_recalled_lessons_in_the_prompt():
    router = FakeRouter([_done_message()])
    executor = RecordingExecutorClient()

    asyncio.run(
        run_coder_task(
            router,
            executor,
            "proj-1",
            "Add a login endpoint",
            recalled_lessons=["Use transactional rollbacks in auth fixtures."],
        )
    )

    sent = router.model.requests[0]
    assert "Task: Add a login endpoint" in sent[1].content
    assert "Use transactional rollbacks in auth fixtures." in sent[1].content


def test_run_coder_task_prompt_is_unchanged_without_recalled_lessons():
    router = FakeRouter([_done_message()])
    executor = RecordingExecutorClient()

    asyncio.run(run_coder_task(router, executor, "proj-1", "trivial task"))

    sent = router.model.requests[0]
    assert sent[1].content == "Task: trivial task"


def test_run_coder_task_streams_partial_content_into_the_progress_buffer():
    router = FakeRouter([AIMessage(content="working on it", tool_calls=[], usage_metadata=usage())])
    executor = RecordingExecutorClient()

    result = asyncio.run(run_coder_task(router, executor, "proj-stream", "trivial task"))

    assert result.success is True
    chunks = get_chunks_since("proj-stream")
    assert "".join(chunk.text for chunk in chunks) == "working on it"


def test_run_coder_task_dispatches_run_command_as_an_argv_list():
    router = FakeRouter(
        [
            _tool_call_message("run_command", {"command": ["python3", "-m", "pytest"]}, "call_1"),
            _done_message(),
        ]
    )
    executor = RecordingExecutorClient()

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "run the tests"))

    assert result.success is True
    assert executor.calls == [("run_command", "proj-1", ("python3", "-m", "pytest"))]
