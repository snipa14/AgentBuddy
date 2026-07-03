"""Unit tests for the Frontend Reviewer agent.

Per .claude/rules/testing.md, mocks at model_router.resolve() boundary; uses
ScriptedProvider for LLM responses and a fake ExecutorClient for screenshots.
Tests cover:
- _parse_frontend_result with approve / changes-requested / invalid verdicts
- review_frontend end-to-end with mocked executor client and provider
- _has_frontend_files detection of frontend extensions
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.orchestrator.agents.frontend_reviewer import (
    _build_findings_text,
    _has_frontend_files,
    _parse_frontend_result,
    review_frontend,
)
from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter, ScriptedProvider
from services.orchestrator.executor_client import ExecutorClient, FileEntry, ScreenshotResult


class RecordingExecutorClient:
    """Fakes ExecutorClient, recording every call made to it."""

    def __init__(self, files: list[str] | None = None, screenshots: list[ScreenshotResult] | None = None):
        self.files = [FileEntry(name=f, is_dir=False) for f in (files or [])]
        self.screenshots = screenshots or [ScreenshotResult(image_b64="iVBORw0KGgo=", error=None)]
        self.calls: list[tuple] = []

    async def list_files(self, project_id: str, path: str = ".") -> list[FileEntry]:
        self.calls.append(("list_files", project_id, path))
        return self.files

    async def take_screenshot(self, project_id, url="http://localhost:5173", **kwargs):
        self.calls.append(("take_screenshot", project_id, url))
        return self.screenshots.pop(0) if self.screenshots else ScreenshotResult(image_b64="", error="no screenshots")


# -- _parse_frontend_result tests ------------------------------------------------

def test_parse_frontend_result_approve():
    content = "## Verdict\napprove\n\n## Findings\n"
    verdict, findings = _parse_frontend_result(content)
    assert verdict == "approve"
    assert findings == []


def test_parse_frontend_result_changes_requested():
    content = (
        "## Verdict\nchanges-requested\n\n"
        "## Findings\n"
        "- line: 10, severity: critical, message: overlapping buttons\n"
        "- line: 25, severity: warning, message: low contrast text\n"
    )
    verdict, findings = _parse_frontend_result(content)
    assert verdict == "changes-requested"
    assert len(findings) == 2
    assert findings[0]["severity"] == "critical"
    assert "overlapping buttons" in findings[0]["message"]
    assert findings[1]["severity"] == "warning"


def test_parse_frontend_result_invalid_verdict():
    content = "## Verdict\nmaybe\n\n## Findings\n"
    from pydantic import ValidationError

    with pytest.raises(ValueError, match="invalid frontend reviewer verdict"):
        _parse_frontend_result(content)


def test_parse_frontend_result_empty_findings_with_changes_requested():
    """changes-requested with no findings is unusual but not rejected by the parser."""
    content = "## Verdict\nchanges-requested\n\n## Findings\n"
    verdict, findings = _parse_frontend_result(content)
    assert verdict == "changes-requested"
    assert findings == []


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

_APPROVE_SCREENSHOT_RESPONSE = "## Verdict\napprove\n\n## Findings\n"

_CHANGES_REQUESTED_SCREENSHOT_RESPONSE = (
    "## Verdict\nchanges-requested\n\n"
    "## Findings\n"
    "- line: 5, severity: critical, message: text unreadable on dark background\n"
)


def test_review_frontend_approves_when_no_screenshots():
    """When all screenshots fail, the agent returns approve with no findings."""
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[ScreenshotResult(image_b64="", error="no_pil")],
    )
    router = FakeRouter([_APPROVE_SCREENSHOT_RESPONSE])

    result_verdict, result_findings = asyncio.run(
        review_frontend(router, executor, "proj-1")
    )

    assert result_verdict == "approve"
    assert result_findings == []


def test_review_frontend_returns_changes_requested_on_finding():
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[ScreenshotResult(image_b64="iVBORw0KGgo=", error=None)],
    )
    router = FakeRouter([_CHANGES_REQUESTED_SCREENSHOT_RESPONSE])

    result_verdict, result_findings = asyncio.run(
        review_frontend(router, executor, "proj-1")
    )

    assert result_verdict == "changes-requested"
    assert len(result_findings) == 1
    assert "unreadable" in result_findings[0]["message"]


def test_review_frontend_calls_take_screenshot():
    """review_frontend takes screenshots at two breakpoints; _has_frontend_files is checked in the node."""
    executor = RecordingExecutorClient(
        files=["index.html"],
        screenshots=[
            ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
            ScreenshotResult(image_b64="iVBORw0KGgo=", error=None),
        ],
    )
    router = FakeRouter([_APPROVE_SCREENSHOT_RESPONSE])

    asyncio.run(review_frontend(router, executor, "proj-1"))

    assert len(executor.calls) == 2  # take_screenshot (desktop) + take_screenshot (mobile)


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
