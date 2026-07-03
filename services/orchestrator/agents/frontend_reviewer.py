"""Frontend Reviewer agent — visual review of UI changes via screenshots.

Takes screenshots of a running dev server inside the workspace container, sends
them to a vision-capable provider (Claude Sonnet by default), and returns
structured findings about layout breakage, accessibility/contrast issues, and
responsiveness problems.

Per .claude/rules/testing.md: mocks the provider at model_router.resolve(), not
deeper. Images are attached as LangChain multimodal content blocks (ADR-025) --
each provider integration (ChatAnthropic/ChatOllama) translates that shared
block format into its own wire format.
"""

import asyncio
import logging
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from services.orchestrator.agents.llm import invoke_structured
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.model_router.router import ModelRouter

logger = logging.getLogger(__name__)

FRONTEND_REVIEWER_ROLE = "frontend_reviewer"

_FRONTEND_SYSTEM_PROMPT = (
    "You are a frontend reviewer. You will receive screenshots of a web application\n"
    "that was just built by a Coder agent inside a workspace container.\n\n"
    "Review each screenshot for:\n"
    "- Layout breakage (overlapping elements, broken grids, truncated text)\n"
    "- Accessibility issues (low contrast text, missing alt text indicators,\n"
    "  focus order problems visible in the layout)\n"
    "- Responsiveness (content clipped at this viewport width, horizontal\n"
    "  scrollbars, elements too small to tap on mobile)\n\n"
    "For each issue found, report an approximate location on the screen, a "
    "severity ('critical' or 'warning'), and a description of what you see. "
    "If there are no issues, the verdict must be 'approve' and findings must "
    "be empty."
)

_FRONTEND_EXTENSIONS = {".html", ".css", ".js", ".ts", ".jsx", ".tsx", ".vue", ".svelte"}


class FrontendFinding(BaseModel):
    """A single frontend review finding with an approximate location, severity, and message."""

    line: int = Field(default=0, description="Approximate location marker (0 if not applicable)")
    severity: str = Field(description="'critical' or 'warning'")
    message: str = Field(description="Description of the visual issue")


class FrontendReviewResult(BaseModel):
    """The frontend reviewer's verdict and structured findings."""

    verdict: str = Field(
        description="'approve' if no issues; 'changes-requested' if at least one finding"
    )
    findings: list[FrontendFinding] = Field(default_factory=list)


def _has_frontend_files(project_id: str, executor_client: ExecutorClient) -> bool:
    """Checks whether project_id contains frontend source files.

    Called from node_frontend_review, a plain sync node function with no
    event loop of its own already running, so a direct asyncio.run() is safe
    here (ADR-019's synchronous-node pattern).

    Args:
        project_id: The project identifier.
        executor_client: The Executor client for listing files in the workspace.

    Returns:
        True if any file with a frontend extension exists in the project root.
    """
    try:
        entries = asyncio.run(executor_client.list_files(project_id, "."))
    except Exception as exc:
        logger.warning(
            "project %s: failed to list files while checking for frontend files: %s",
            project_id, exc,
        )
        return False
    return any(Path(e.name).suffix.lower() in _FRONTEND_EXTENSIONS for e in entries)


async def review_frontend(
    router: ModelRouter,
    executor_client: ExecutorClient,
    project_id: str,
    url: str = "http://localhost:5173",
) -> tuple[str, list[dict], AIMessage | None]:
    """Reviews UI screenshots and returns a structured verdict with findings.

    Takes screenshots of the running dev server at *url*, sends them to a
    vision-capable provider, and gets back a schema-forced result.

    Args:
        router: The ModelRouter used to resolve the 'frontend_reviewer' role's
            provider (must support vision capability).
        executor_client: The Executor client for capturing screenshots inside
            the workspace container.
        project_id: The project identifier (used in logging).
        url: The URL of the running dev server to capture.

    Returns:
        A tuple of (verdict, findings, raw AIMessage) where verdict is
        'approve' or 'changes-requested', findings is a list of dicts with
        keys 'line', 'severity', and 'message', and raw AIMessage is None
        when no screenshots were captured (no LLM call was made) or carries
        usage_metadata for cost tracking otherwise.

    Raises:
        ValueError: If the model's response doesn't match FrontendReviewResult.
        NoCapableProviderError: If no vision-capable provider is configured.
    """
    llm = router.resolve(FRONTEND_REVIEWER_ROLE)

    # Take screenshots at multiple breakpoints. Each take_screenshot call runs
    # inside a fresh, ephemeral workspace container (ARCHITECTURE.md §4.3), so
    # nothing is ever already listening at *url* -- auto_start_server=True is
    # required for a screenshot to ever succeed against a real project.
    breakpoints: list[tuple[str, int]] = [
        ("desktop", 1280),
        ("mobile", 375),
    ]
    screenshots: list[tuple[str, str]] = []
    for label, width in breakpoints:
        result = await executor_client.take_screenshot(
            project_id, url=url, auto_start_server=True, breakpoint_width=width
        )
        if result.error:
            logger.warning(
                "project %s: screenshot (%s) failed: %s", project_id, label, result.error
            )
        else:
            screenshots.append((label, result.image_b64))

    if not screenshots:
        logger.warning(
            "project %s: no screenshots captured, skipping frontend review", project_id
        )
        return "approve", [], None

    content_blocks: list[dict] = [
        {
            "type": "text",
            "text": "Screenshots attached, in order: " + ", ".join(label for label, _ in screenshots),
        }
    ]
    for _, image_b64 in screenshots:
        content_blocks.append(
            {
                "type": "image",
                "source_type": "base64",
                "data": image_b64,
                "mime_type": "image/png",
            }
        )

    messages = [
        SystemMessage(content=_FRONTEND_SYSTEM_PROMPT),
        HumanMessage(content=content_blocks),
    ]
    result, ai_message = await invoke_structured(llm, FrontendReviewResult, messages)

    findings = [finding.model_dump() for finding in result.findings]
    logger.info(
        "project %s: frontend review verdict=%s, findings=%d",
        project_id, result.verdict, len(findings),
    )
    return result.verdict, findings, ai_message


def _build_findings_text(findings: list[dict]) -> str:
    """Formats findings into a human-readable string for attachment to messages."""
    lines = ["Frontend review feedback:"]
    for f in findings:
        lines.append(f"- line: {f['line']} [{f['severity']}] {f['message']}")
    return "\n".join(lines)
