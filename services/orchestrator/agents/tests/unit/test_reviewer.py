"""Unit tests for the Reviewer agent.

Per .claude/rules/testing.md, LLM-backed agent tests mock the provider at
the model_router.resolve() boundary (router_fakes.py). A RecordingExecutorClient
fakes the Executor side and records every call made to it.
"""

import asyncio

import pytest

from services.orchestrator.agents.reviewer import (
    ReviewFinding,
    ReviewResult,
    _parse_review_result,
    review_changes,
)
from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter


APPROVE_RESPONSE = "## Verdict\napprove\n\n## Findings\n"

CHANGES_REQUESTED_RESPONSE = (
    "## Verdict\nchanges-requested\n\n"
    "## Findings\n"
    "- file: models.py, line: 15, severity: critical, message: hardcoded password in source code\n"
    "- file: views.py, line: 42, severity: warning, message: missing null check on user lookup\n"
)

INVALID_VERDICT_RESPONSE = "## Verdict\nmaybe_later\n\n## Findings\n"


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


def test_parse_review_result_approve():
    result = _parse_review_result(APPROVE_RESPONSE)

    assert result.verdict == "approve"
    assert result.findings == []


def test_parse_review_result_changes_requested_with_findings():
    result = _parse_review_result(CHANGES_REQUESTED_RESPONSE)

    assert result.verdict == "changes-requested"
    assert len(result.findings) == 2
    assert result.findings[0] == ReviewFinding(
        file="models.py", line=15, severity="critical", message="hardcoded password in source code"
    )
    assert result.findings[1] == ReviewFinding(
        file="views.py", line=42, severity="warning", message="missing null check on user lookup"
    )


def test_parse_review_result_raises_on_invalid_verdict():
    with pytest.raises(ValueError, match="invalid reviewer verdict"):
        _parse_review_result(INVALID_VERDICT_RESPONSE)


def test_parse_review_result_drops_bullet_with_invalid_severity(caplog):
    response = (
        "## Verdict\nchanges-requested\n\n"
        "## Findings\n"
        "- file: models.py, line: 15, severity: nitpick, message: not a real severity\n"
        "- file: views.py, line: 42, severity: warning, message: this one is fine\n"
    )

    with caplog.at_level("WARNING"):
        result = _parse_review_result(response)

    assert result.verdict == "changes-requested"
    assert len(result.findings) == 1
    assert result.findings[0].file == "views.py"
    assert "malformed" in caplog.text.lower()


def test_parse_review_result_drops_bullet_missing_file(caplog):
    response = (
        "## Verdict\nchanges-requested\n\n"
        "## Findings\n"
        "- line: 15, severity: critical, message: no file given\n"
    )

    with caplog.at_level("WARNING"):
        result = _parse_review_result(response)

    assert result.verdict == "changes-requested"
    assert result.findings == []
    assert "malformed" in caplog.text.lower()


def test_review_changes_approves_clean_files():
    router = FakeRouter([APPROVE_RESPONSE])
    executor = RecordingExecutorClient(files={"models.py": "class User: pass\n"})

    result = asyncio.run(review_changes(router, executor, "proj-1"))

    assert result.verdict == "approve"
    assert result.findings == []


def test_review_changes_flags_hardcoded_secret():
    router = FakeRouter([CHANGES_REQUESTED_RESPONSE])
    executor = RecordingExecutorClient(
        files={
            "models.py": "class User:\n    password = 'admin123'\n",
            "views.py": "user = get_user(id)\nreturn user.name\n",
        }
    )

    result = asyncio.run(review_changes(router, executor, "proj-1"))

    assert result.verdict == "changes-requested"
    assert len(result.findings) == 2


def test_review_changes_reads_only_changed_files_when_listed():
    router = FakeRouter([APPROVE_RESPONSE])
    executor = RecordingExecutorClient(
        files={
            "models.py": "class User: pass\n",
            "views.py": "# old content\n",
        }
    )

    result = asyncio.run(review_changes(router, executor, "proj-1", changed_files=["models.py"]))

    assert result.verdict == "approve"
    # When changed_files is provided, skip list_files and read only those files
    assert executor.calls == [("read_file", "proj-1", "models.py")]


def test_review_changes_skips_unreadable_files_gracefully():
    router = FakeRouter([APPROVE_RESPONSE])

    class FailingExecutorClient:
        async def list_files(self, project_id, path="."):
            from services.orchestrator.executor_client import FileEntry

            return [FileEntry(name="models.py", is_dir=False), FileEntry(name="bad.py", is_dir=False)]

        async def read_file(self, project_id, path):
            if path == "bad.py":
                raise FileNotFoundError("nope")
            return "class User: pass\n"

    result = asyncio.run(review_changes(router, FailingExecutorClient(), "proj-1"))

    assert result.verdict == "approve"
