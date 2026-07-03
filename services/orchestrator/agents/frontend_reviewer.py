"""Frontend Reviewer agent — visual review of UI changes via screenshots.

Takes screenshots of a running dev server inside the workspace container, sends
them to a vision-capable provider (Claude Sonnet by default), and returns
structured findings about layout breakage, accessibility/contrast issues, and
responsiveness problems.

Per .claude/rules/testing.md: mocks the provider at model_router.resolve(), not
deeper. Uses base64-encoded images in CompletionMessage for vision support.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.orchestrator.agents.parsing import bullet_lines, split_sections
from services.orchestrator.executor_client import ExecutorClient, ScreenshotResult
from services.orchestrator.model_router.router import ModelRouter

logger = logging.getLogger(__name__)

_FRONTEND_SYSTEM_PROMPT = (
    "You are a frontend reviewer. You will receive screenshots of a web application\n"
    "that was just built by a Coder agent inside a workspace container.\n\n"
    "Review each screenshot for:\n"
    "- Layout breakage (overlapping elements, broken grids, truncated text)\n"
    "- Accessibility issues (low contrast text, missing alt text indicators,\n"
    "  focus order problems visible in the layout)\n"
    "- Responsiveness (content clipped at this viewport width, horizontal\n"
    "  scrollbars, elements too small to tap on mobile)\n\n"
    "For each issue found, provide: a description of what you see, severity\n"
    "(critical / warning), and an approximate location on the screen.\n\n"
    "Return your response in exactly this format:\n"
    "## Verdict\napprove or changes-requested\n\n"
    "## Findings\n- line: N, severity: critical, message: description\n"
    "- one bullet per finding\n\n"
    "If there are no findings, verdict must be 'approve' and Findings must be empty."
)

_FRONTEND_EXTENSIONS = {".html", ".css", ".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte"}


def _has_frontend_files(project_id: str, executor_client: ExecutorClient) -> bool:
    """Checks whether project_id contains frontend source files.

    Runs the async list_files call inside a ThreadPoolExecutor so it can be
    used from sync node functions and routing predicates without conflicting
    with an outer asyncio.run() context.

    Args:
        project_id: The project identifier.
        executor_client: The Executor client for listing files in the workspace.

    Returns:
        True if any file with a frontend extension exists in the project root.
    """
    try:
        with ThreadPoolExecutor() as pool:
            future = pool.submit(
                _async_run,
                lambda: executor_client.list_files(project_id, "."),
            )
            entries = future.result()
    except Exception:
        return False
    return any(Path(e.name).suffix.lower() in _FRONTEND_EXTENSIONS for e in entries)


def _async_run(coro_factory):
    """Runs a coroutine created by coro_factory() inside a ThreadPoolExecutor thread."""
    return asyncio.run(coro_factory())


def _build_image_message(screenshots: list[tuple[str, str]]) -> dict:
    """Builds a user message with text prompt and attached images.

    Args:
        screenshots: A list of (label, base64_data) tuples for each screenshot.

    Returns:
        A dict with 'text' and 'images' keys suitable for the frontend review.
    """
    parts = ["You are reviewing screenshots of a web application.\n\n"]
    for label, _ in screenshots:
        parts.append(f"[{label}]\n")
    return {"system": _FRONTEND_SYSTEM_PROMPT, "user": "\n".join(parts)}


async def review_frontend(
    router: ModelRouter,
    executor_client: ExecutorClient,
    project_id: str,
    url: str = "http://localhost:5173",
) -> tuple[str, list[dict]]:
    """Reviews UI screenshots and returns a structured verdict with findings.

    Takes screenshots of the running dev server at *url*, sends them to a
    vision-capable provider, parses the structured response.

    Args:
        router: The ModelRouter used to resolve the 'frontend_reviewer' role's
            provider (must support vision capability).
        executor_client: The Executor client for capturing screenshots inside
            the workspace container.
        project_id: The project identifier (used in logging).
        url: The URL of the running dev server to capture.

    Returns:
        A tuple of (verdict, findings) where verdict is 'approve' or
        'changes-requested', and findings is a list of dicts with keys
        'line', 'severity', and 'message'.

    Raises:
        ValueError: If the LLM returns an unrecognized verdict format.
        NoCapableProviderError: If no vision-capable provider is configured.
    """
    provider = router.resolve("frontend_reviewer")

    # Take screenshots at multiple breakpoints
    breakpoints: list[tuple[str, int]] = [
        ("desktop", 1280),
        ("mobile", 375),
    ]
    screenshot_results: list[ScreenshotResult] = []
    for label, width in breakpoints:
        result = await executor_client.take_screenshot(
            project_id, url=url, breakpoint_width=width
        )
        if result.error:
            logger.warning(
                "project %s: screenshot (%s) failed: %s", project_id, label, result.error
            )
        else:
            screenshot_results.append(result)

    if not screenshot_results:
        logger.warning("project %s: no screenshots captured, skipping frontend review")
        return "approve", []

    # Build the vision request with base64-encoded images
    images = []
    for i, sr in enumerate(screenshot_results):
        images.append(("Screenshot {i}".format(i=i), sr.image_b64))

    messages_data = _build_image_message(images)

    # Encode images into CompletionMessage format
    from services.orchestrator.model_router.schemas import (
        CompletionMessage,
        ImageContent,
        CompletionRequest,
    )

    image_msgs = []
    for label, b64 in images:
        image_msgs.append(
            CompletionMessage(
                role="user",
                content=label,
                images=[ImageContent(data=b64)],
            )
        )

    response = await provider.complete(
        CompletionRequest(messages=image_msgs)
    )

    verdict, findings = _parse_frontend_result(response.content)
    logger.info(
        "project %s: frontend review verdict=%s, findings=%d",
        project_id, verdict, len(findings),
    )
    return verdict, findings


def _parse_frontend_result(content: str) -> tuple[str, list[dict]]:
    """Parses the LLM's structured response into a (verdict, findings) tuple.

    Args:
        content: The raw LLM response text with ## Verdict and ## Findings
            sections.

    Returns:
        A tuple of (verdict, findings) where verdict is 'approve' or
        'changes-requested', and findings is a list of dicts with keys
        'line', 'severity', and 'message'.

    Raises:
        ValueError: If the verdict is not 'approve' or 'changes-requested'.
    """
    sections = split_sections(content)

    verdict_raw = sections.get("Verdict", "").strip().lower()
    if verdict_raw not in ("approve", "changes-requested"):
        raise ValueError(
            f"invalid frontend reviewer verdict: {verdict_raw!r}; "
            "expected 'approve' or 'changes-requested'"
        )

    findings: list[dict] = []
    for bullet in bullet_lines(sections.get("Findings", "")):
        line_match = None
        severity_match = None
        message_parts: list[str] = []

        parts = [p.strip() for p in bullet.split(",")]
        for part in parts:
            if part.startswith("line:"):
                try:
                    line_match = int(part.removeprefix("line:").strip())
                except ValueError:
                    line_match = 0
            elif part.startswith("severity:"):
                severity_match = part.removeprefix("severity:").strip().lower()
            else:
                message_parts.append(part)

        if severity_match in ("critical", "warning") and message_parts:
            raw_message = " ".join(message_parts).strip()
            if raw_message.lower().startswith("message:"):
                raw_message = raw_message.removeprefix("message:").strip()
            findings.append(
                {
                    "line": line_match or 0,
                    "severity": severity_match,
                    "message": raw_message,
                }
            )

    return verdict_raw, findings


def _build_findings_text(findings: list[dict]) -> str:
    """Formats findings into a human-readable string for attachment to messages."""
    lines = ["Frontend review feedback:"]
    for f in findings:
        lines.append(f"- line: {f['line']} [{f['severity']}] {f['message']}")
    return "\n".join(lines)
