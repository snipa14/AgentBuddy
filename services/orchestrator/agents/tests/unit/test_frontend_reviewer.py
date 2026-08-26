"""Unit tests for the Frontend Reviewer agent.

Per .claude/rules/testing.md, mocks at model_router.resolve() boundary; uses
FakeRouter/FakeChatModel for LLM responses and a fake ExecutorClient for
screenshots. Tests cover:
- _has_frontend_files detection of frontend extensions
- review_frontend end-to-end with mocked executor client and provider
- _build_findings_text formatting
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from services.orchestrator.agents.frontend_reviewer import (
    FrontendFinding,
    FrontendReviewResult,
    _build_findings_text,
    _has_frontend_files,
    review_frontend,
)
from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter, structured
from services.orchestrator.executor_client import FileEntry, ScreenshotResult


class RecordingExecutorClient:
    """Fakes ExecutorClient, recording every call made to it."""

    def __init__(
        self, files: list[str] | None = None, screenshots: list[ScreenshotResult] | None = None
    ):
        self.files = [FileEntry(name=f, is_dir=False) for f in (files or [])]
        self.screenshots = screenshots or [ScreenshotResult(image_b64="iVBORw0KGgo=", error=None)]
        self.calls: list[tuple] = []

    async def list_files(
        self, project_id: str, path: str = ".", stack: str = "python"
    ) -> list[FileEntry]:
        self.calls.append(("list_files", project_id, path))
        return self.files

    async def take_screenshot(self, project_id, url="http://localhost:5173", **kwargs):
        self.calls.append(("take_screenshot", project_id, url, kwargs))
        return (
            self.screenshots.pop(0)
            if self.screenshots
            else ScreenshotResult(image_b64="", error="no screenshots")
        )


# -- _has_frontend_files tests ---------------------------------------------------


def test_has_frontend_files_detects_html():
    executor = RecordingExecutorClient(files=["index.html", "main.py"])
    assert _has_frontend_files("proj-1", executor) is True


def test_has_frontend_files_detects_jsx():
    executor = RecordingExecutorClient(files=["App.jsx", "styles.css"])
    assert _has_frontend_files("proj-1", executor) is True


def test_has_frontend_files_no_frontend_files():
    executor = RecordingExecutorClient(files=["main.py", "requirements.txt"])
    assert _has_frontend_files("proj-1", executor) is False


def test_has_frontend_files_handles_list_error():
    executor = MagicMock()
    executor.list_files = AsyncMock(side_effect=Exception("docker error"))
    assert _has_frontend_files("proj-1", executor) is False


# -- review_frontend end-to-end tests ----------------------------------------------

APPROVE_RESULT = FrontendReviewResult(verdict="approve", findings=[])

CHANGES_REQUESTED_RESULT = FrontendReviewResult(
    verdict="changes-requested",
    findings=[
        FrontendFinding(line=5, severity="critical", message="text unreadable on dark background")
    ],
)


def test_review_frontend_requests_changes_when_no_screenshots():
    """When all screenshots fail, the agent fails closed with changes-requested
    and a critical finding instead of silently approving a UI it never saw --
    no LLM call is made either way."""
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[ScreenshotResult(image_b64="", error="no_playwright")],
    )
    router = FakeRouter([])

    verdict, findings, message = asyncio.run(review_frontend(router, executor, "proj-1"))

    assert verdict == "changes-requested"
    assert len(findings) == 1
    assert findings[0]["severity"] == "critical"
    assert message is None


def test_review_frontend_returns_changes_requested_on_finding():
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[ScreenshotResult(image_b64="iVBORw0KGgo=", error=None)],
    )
    router = FakeRouter([structured(CHANGES_REQUESTED_RESULT)])

    verdict, findings, message = asyncio.run(review_frontend(router, executor, "proj-1"))

    assert verdict == "changes-requested"
    assert len(findings) == 1
    assert "unreadable" in findings[0]["message"]
    assert message.usage_metadata is not None


def test_review_frontend_calls_take_screenshot_at_two_breakpoints():
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[
            ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
            ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
        ],
    )
    router = FakeRouter([structured(APPROVE_RESULT)])

    asyncio.run(review_frontend(router, executor, "proj-1"))

    assert len(executor.calls) == 2  # take_screenshot (desktop) + take_screenshot (mobile)


def test_review_frontend_auto_starts_the_dev_server():
    """Every take_screenshot call runs in a fresh, ephemeral container (no
    server is ever already listening), so auto_start_server must be True on
    every call or screenshots can never succeed against a real project."""
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[
            ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
            ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
        ],
    )
    router = FakeRouter([structured(APPROVE_RESULT)])

    asyncio.run(review_frontend(router, executor, "proj-1"))

    take_screenshot_calls = [c for c in executor.calls if c[0] == "take_screenshot"]
    assert len(take_screenshot_calls) == 2
    for call in take_screenshot_calls:
        assert call[3]["auto_start_server"] is True


def test_review_frontend_sends_a_system_prompt_with_the_images():
    """The model must actually be told what to look for and be sent both images."""
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[
            ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
            ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
        ],
    )
    router = FakeRouter([structured(APPROVE_RESULT)])

    asyncio.run(review_frontend(router, executor, "proj-1"))

    sent_messages = router.model.requests[0]
    system_message = sent_messages[0]
    assert "layout breakage" in system_message.content.lower()

    user_message = sent_messages[1]
    image_blocks = [block for block in user_message.content if block.get("type") == "image"]
    assert len(image_blocks) == 2


def test_review_frontend_system_prompt_includes_skill_body():
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[ScreenshotResult(image_b64="iVBORw0KGgo=", error=None)],
    )
    router = FakeRouter([structured(APPROVE_RESULT)])

    asyncio.run(review_frontend(router, executor, "proj-1"))

    system_message = router.model.requests[0][0]
    assert "Layout breakage" in system_message.content


def test_review_frontend_system_prompt_clarifies_no_clean_category_findings():
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[ScreenshotResult(image_b64="iVBORw0KGgo=", error=None)],
    )
    router = FakeRouter([structured(APPROVE_RESULT)])

    asyncio.run(review_frontend(router, executor, "proj-1"))

    system_message = router.model.requests[0][0]
    assert "not a finding" in system_message.content
    assert "empty findings list" in system_message.content


# -- _build_findings_text tests ----------------------------------------------------


def test_build_findings_text_formats_single_finding():
    findings = [{"line": 42, "severity": "critical", "message": "broken layout"}]
    text = _build_findings_text(findings)
    assert "Frontend review feedback:" in text
    assert "[critical]" in text
    assert "broken layout" in text


def test_build_findings_text_formats_multiple():
    findings = [
        {"line": 10, "severity": "warning", "message": "low contrast"},
        {"line": 20, "severity": "critical", "message": "overlapping elements"},
    ]
    text = _build_findings_text(findings)
    assert text.count("[warning]") == 1
    assert text.count("[critical]") == 1
