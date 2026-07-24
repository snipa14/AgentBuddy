"""Unit tests for the Reviewer agent.

Per .claude/rules/testing.md, LLM-backed agent tests mock the provider at
the model_router.resolve() boundary (router_fakes.py). A RecordingExecutorClient
fakes the Executor side and records every call made to it.
"""

import asyncio

from services.orchestrator.agents.reviewer import ReviewFinding, ReviewResult, review_changes
from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter, structured

APPROVE_RESULT = ReviewResult(verdict="approve", findings=[])

CHANGES_REQUESTED_RESULT = ReviewResult(
    verdict="changes-requested",
    findings=[
        ReviewFinding(
            file="models.py", line=15, severity="critical",
            message="hardcoded password in source code",
        ),
        ReviewFinding(
            file="views.py", line=42, severity="warning",
            message="missing null check on user lookup",
        ),
    ],
)


class RecordingExecutorClient:
    """Fakes ExecutorClient, recording every call made to it in order."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.calls: list[tuple] = []

    async def read_file(self, project_id: str, path: str) -> str:
        self.calls.append(("read_file", project_id, path))
        if path not in self.files:
            raise FileNotFoundError(f"'{path}' not found")
        return self.files[path]

    async def list_files(self, project_id: str, path: str = "."):
        self.calls.append(("list_files", project_id, path))
        from services.orchestrator.executor_client import FileEntry

        return [FileEntry(name=name, is_dir=False) for name in self.files]


def test_review_changes_approves_clean_files():
    router = FakeRouter([structured(APPROVE_RESULT)])
    executor = RecordingExecutorClient(files={"models.py": "class User: pass\n"})

    result, message = asyncio.run(review_changes(router, executor, "proj-1"))

    assert result.verdict == "approve"
    assert result.findings == []
    assert message.usage_metadata is not None


def test_review_changes_flags_hardcoded_secret():
    router = FakeRouter([structured(CHANGES_REQUESTED_RESULT)])
    executor = RecordingExecutorClient(
        files={
            "models.py": "class User:\n    password = 'admin123'\n",
            "views.py": "user = get_user(id)\nreturn user.name\n",
        }
    )

    result, _ = asyncio.run(review_changes(router, executor, "proj-1"))

    assert result.verdict == "changes-requested"
    assert len(result.findings) == 2
    assert result.findings[0] == ReviewFinding(
        file="models.py", line=15, severity="critical", message="hardcoded password in source code"
    )


def test_review_changes_reads_only_changed_files_when_listed():
    router = FakeRouter([structured(APPROVE_RESULT)])
    executor = RecordingExecutorClient(
        files={
            "models.py": "class User: pass\n",
            "views.py": "# old content\n",
        }
    )

    result, _ = asyncio.run(
        review_changes(router, executor, "proj-1", changed_files=["models.py"])
    )

    assert result.verdict == "approve"
    # When changed_files is provided, skip list_files and read only those files
    assert executor.calls == [("read_file", "proj-1", "models.py")]


def test_review_changes_skips_unreadable_files_gracefully():
    router = FakeRouter([structured(APPROVE_RESULT)])

    class FailingExecutorClient:
        async def list_files(self, project_id, path="."):
            from services.orchestrator.executor_client import FileEntry

            return [FileEntry(name="models.py", is_dir=False), FileEntry(name="bad.py", is_dir=False)]

        async def read_file(self, project_id, path):
            if path == "bad.py":
                raise FileNotFoundError("nope")
            return "class User: pass\n"

    result, _ = asyncio.run(review_changes(router, FailingExecutorClient(), "proj-1"))

    assert result.verdict == "approve"


def test_review_changes_sends_system_and_user_messages():
    router = FakeRouter([structured(APPROVE_RESULT)])
    executor = RecordingExecutorClient(files={"models.py": "class User: pass\n"})

    asyncio.run(review_changes(router, executor, "proj-1"))

    sent_messages = router.model.requests[0]
    assert sent_messages[0].content.lower().count("security") >= 1
    assert "models.py" in sent_messages[1].content


def test_review_result_defaults_to_no_replan():
    result = ReviewResult(verdict="approve")
    assert result.replan_needed is False
    assert result.replan_reason == ""


def test_review_result_carries_replan_signal():
    result = ReviewResult(
        verdict="changes-requested",
        replan_needed=True,
        replan_reason="Task 3 assumes an API endpoint the plan never provisions",
    )
    assert result.replan_needed is True
    assert result.replan_reason
