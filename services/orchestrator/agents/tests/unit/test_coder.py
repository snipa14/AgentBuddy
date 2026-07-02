"""Unit tests for the Coder agent.

Per .claude/rules/testing.md, LLM-backed agent tests mock the provider at
the model_router.resolve() boundary (router_fakes.py, shared with
test_planner.py). RecordingExecutorClient fakes the Executor side and
records every call made to it, so tests can assert on *how* the Coder
worked (the exact sequence of tool calls), not just the final file state --
per TODO.md Phase 9's explicit ask.
"""

import asyncio

import pytest

from services.orchestrator.agents.coder import (
    decide_next_action,
    decompose_plan_into_tasks,
    run_coder_task,
)
from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter
from services.orchestrator.executor_client import CommandResult, ExecutorClientError

DECOMPOSE_RESPONSE = (
    "## Tasks\n"
    "Add a User model with email and password_hash fields\n"
    "Add a POST /login endpoint\n"
)

READ_FILE_ACTION_RESPONSE = "## Action\nread_file\n\n## Path\nmodels.py\n"
WRITE_FILE_ACTION_RESPONSE = (
    "## Action\nwrite_file\n\n## Path\nmodels.py\n\n## Content\nclass User:\n    pass\n"
)
RUN_COMMAND_ACTION_RESPONSE = "## Action\nrun_command\n\n## Command\npython3\n-m\npytest\n"
DONE_ACTION_RESPONSE = "## Action\ndone\n"


class RecordingExecutorClient:
    """Fakes ExecutorClient, recording every call made to it in order."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.calls: list[tuple] = []

    async def read_file(self, project_id: str, path: str) -> str:
        self.calls.append(("read_file", project_id, path))
        if path not in self.files:
            raise ExecutorClientError(f"404: '{path}' not found")
        return self.files[path]

    async def write_file(self, project_id: str, path: str, content: str) -> bool:
        self.calls.append(("write_file", project_id, path, content))
        self.files[path] = content
        return True

    async def run_command(self, project_id, command, timeout=None):
        self.calls.append(("run_command", project_id, tuple(command)))
        return CommandResult(stdout="3 passed", stderr="", exit_code=0)


def test_decompose_plan_into_tasks_parses_task_list():
    router = FakeRouter([DECOMPOSE_RESPONSE])

    tasks = asyncio.run(
        decompose_plan_into_tasks(router, {"summary": "An auth service", "goals": ["login"]})
    )

    assert [task.description for task in tasks] == [
        "Add a User model with email and password_hash fields",
        "Add a POST /login endpoint",
    ]


def test_decompose_plan_into_tasks_raises_on_no_tasks():
    router = FakeRouter(["## Tasks\n"])

    with pytest.raises(ValueError, match="no tasks found"):
        asyncio.run(decompose_plan_into_tasks(router, {"summary": "x"}))


def test_decide_next_action_parses_read_file():
    router = FakeRouter([READ_FILE_ACTION_RESPONSE])

    action = asyncio.run(decide_next_action(router, "implement the User model", []))

    assert action.kind == "read_file"
    assert action.path == "models.py"


def test_decide_next_action_parses_write_file():
    router = FakeRouter([WRITE_FILE_ACTION_RESPONSE])

    action = asyncio.run(decide_next_action(router, "implement the User model", []))

    assert action.kind == "write_file"
    assert action.path == "models.py"
    assert action.content == "class User:\n    pass"


def test_decide_next_action_parses_run_command():
    router = FakeRouter([RUN_COMMAND_ACTION_RESPONSE])

    action = asyncio.run(decide_next_action(router, "run the tests", []))

    assert action.kind == "run_command"
    assert action.command == ["python3", "-m", "pytest"]


def test_decide_next_action_parses_done():
    router = FakeRouter([DONE_ACTION_RESPONSE])

    action = asyncio.run(decide_next_action(router, "implement the User model", []))

    assert action.kind == "done"


def test_decide_next_action_raises_on_malformed_action():
    router = FakeRouter(["## Action\nfrobnicate\n"])

    with pytest.raises(ValueError, match="malformed coder action"):
        asyncio.run(decide_next_action(router, "x", []))


def test_run_coder_task_reads_then_writes_then_finishes():
    router = FakeRouter(
        [READ_FILE_ACTION_RESPONSE, WRITE_FILE_ACTION_RESPONSE, DONE_ACTION_RESPONSE]
    )
    executor = RecordingExecutorClient(files={"models.py": "# empty\n"})

    result = asyncio.run(
        run_coder_task(router, executor, "proj-1", "Add a User model")
    )

    assert result.success is True
    assert executor.calls == [
        ("read_file", "proj-1", "models.py"),
        ("write_file", "proj-1", "models.py", "class User:\n    pass"),
    ]
    assert executor.files["models.py"] == "class User:\n    pass"


def test_run_coder_task_records_a_failed_read_and_still_recovers():
    router = FakeRouter(
        [READ_FILE_ACTION_RESPONSE, WRITE_FILE_ACTION_RESPONSE, DONE_ACTION_RESPONSE]
    )
    executor = RecordingExecutorClient(files={})

    result = asyncio.run(run_coder_task(router, executor, "proj-1", "Add a User model"))

    assert result.success is True
    assert executor.calls[0] == ("read_file", "proj-1", "models.py")
    assert "error: 404" in result.transcript[0]
    assert executor.calls[1] == ("write_file", "proj-1", "models.py", "class User:\n    pass")


def test_run_coder_task_returns_failure_when_max_steps_exhausted():
    router = FakeRouter([READ_FILE_ACTION_RESPONSE] * 3)
    executor = RecordingExecutorClient(files={"models.py": "# empty\n"})

    result = asyncio.run(
        run_coder_task(router, executor, "proj-1", "Add a User model", max_steps=3)
    )

    assert result.success is False
    assert len(result.transcript) == 3
    assert len(executor.calls) == 3
