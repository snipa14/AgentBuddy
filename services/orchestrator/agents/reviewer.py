"""Reviewer agent — inspects code changes and returns a structured verdict.

Runs after the Coder finishes all tasks, before tests execute. Reads every file
in the project, sends it to an LLM with a review prompt, parses structured
output (verdict + findings), and routes back to Coder if changes are requested.

Per .claude/rules/testing.md: mocks the provider at model_router.resolve(), not
deeper. Uses the same ## Heading section parsing as Planner/Coder via
agents/parsing.py.
"""

import logging

from pydantic import BaseModel, Field

from services.orchestrator.agents.parsing import bullet_lines, split_sections
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.model_router.schemas import CompletionMessage, CompletionRequest

logger = logging.getLogger(__name__)

_REVIEW_SYSTEM_PROMPT = (
    "You are a code reviewer. You will receive the contents of files that were "
    "just written by a Coder agent inside a workspace container.\n\n"
    "Review each file for:\n"
    "- Security issues (hardcoded secrets, injection vulnerabilities, missing null checks)\n"
    "- Logic errors (off-by-one, unhandled edge cases, incorrect assumptions)\n"
    "- Scope creep (code outside the stated plan goals)\n\n"
    "For each issue found, provide: file name, line number (approximate is fine), "
    "severity (critical / warning), and a concise message.\n\n"
    "Return your response in exactly this format:\n"
    "## Verdict\napprove or changes-requested\n\n"
    "## Findings\n- file: path/to/file.py, line: 42, severity: critical, message: description\n"
    "- one bullet per finding\n\n"
    "If there are no findings, verdict must be 'approve' and Findings must be empty."
)


class ReviewFinding(BaseModel):
    """A single review finding with file, line, severity, and message."""

    file: str = Field(description="File path where the issue was found")
    line: int = Field(description="Approximate line number of the issue")
    severity: str = Field(description="'critical' or 'warning'")
    message: str = Field(description="Concise description of the issue")


class ReviewResult(BaseModel):
    """The reviewer's verdict and structured findings."""

    verdict: str = Field(
        description="'approve' if no issues; 'changes-requested' if at least one finding"
    )
    findings: list[ReviewFinding] = Field(default_factory=list)


def _build_user_prompt(project_id: str, files: dict[str, str]) -> str:
    """Builds the user prompt with file contents for review."""
    parts = [f"Project: {project_id}\n\n"]
    for path, content in sorted(files.items()):
        parts.append(f"### {path}\n```\n{content}\n```\n")
    return "\n".join(parts)


def _parse_review_result(content: str) -> ReviewResult:
    """Parses the LLM's structured response into a ReviewResult.

    Args:
        content: The raw LLM response text with ## Verdict and ## Findings sections.

    Returns:
        A validated ReviewResult. Any individual finding bullet that's missing
        its file, has an unrecognized severity, or has no message text is
        dropped (and logged as a warning) rather than failing the whole
        response -- one malformed bullet from the LLM shouldn't discard every
        other finding it reported correctly.

    Raises:
        ValueError: If the verdict is not 'approve' or 'changes-requested'.
    """
    sections = split_sections(content)

    verdict_raw = sections.get("Verdict", "").strip().lower()
    if verdict_raw not in ("approve", "changes-requested"):
        raise ValueError(
            f"invalid reviewer verdict: {verdict_raw!r}; "
            "expected 'approve' or 'changes-requested'"
        )

    findings = []
    for bullet in bullet_lines(sections.get("Findings", "")):
        file_match = None
        line_match = None
        severity_match = None
        message_parts = []

        # Parse inline format: "file: path, line: N, severity: X, message: text"
        parts = [p.strip() for p in bullet.split(",")]
        for part in parts:
            if part.startswith("file:"):
                file_match = part.removeprefix("file:").strip()
            elif part.startswith("line:"):
                try:
                    line_match = int(part.removeprefix("line:").strip())
                except ValueError:
                    line_match = 0
            elif part.startswith("severity:"):
                severity_match = part.removeprefix("severity:").strip().lower()
            else:
                message_parts.append(part)

        if not (file_match and severity_match in ("critical", "warning") and message_parts):
            logger.warning("dropping malformed reviewer finding bullet: %r", bullet)
            continue

        raw_message = " ".join(message_parts).strip()
        # Strip leading 'message:' prefix if present (from the last comma-split part)
        if raw_message.lower().startswith("message:"):
            raw_message = raw_message.removeprefix("message:").strip()
        findings.append(
            ReviewFinding(
                file=file_match,
                line=line_match or 0,
                severity=severity_match,
                message=raw_message,
            )
        )

    return ReviewResult(verdict=verdict_raw, findings=findings)


async def review_changes(
    router: ModelRouter,
    executor_client,
    project_id: str,
    changed_files: list[str] | None = None,
) -> ReviewResult:
    """Reviews code changes and returns a structured verdict.

    Reads all files in the project (or only the listed changed files if provided),
    sends them to an LLM with a review prompt, parses the structured response.

    Args:
        router: The ModelRouter used to resolve the 'reviewer' role's provider.
        executor_client: The Executor client for reading file contents inside
            the workspace container.
        project_id: The project identifier (used as thread_id and in logging).
        changed_files: Optional list of file paths that were modified during coding.
            If None, all files in the project root are reviewed.

    Returns:
        A ReviewResult with verdict ('approve' or 'changes-requested') and
        structured findings if any issues were found.

    Raises:
        ValueError: If the LLM returns an unrecognized verdict format.
    """
    provider = router.resolve("reviewer")

    # Read file contents from the workspace container
    files: dict[str, str] = {}
    if changed_files:
        for path in changed_files:
            try:
                content = await executor_client.read_file(project_id, path)
                files[path] = content
            except Exception as exc:
                logger.warning("project %s: failed to read '%s' for review: %s", project_id, path, exc)
    else:  # no changed_files list — list all and read each one
        # List all files in the project root and read each one
        try:
            entries = await executor_client.list_files(project_id, ".")
            for entry in entries:
                if not entry.is_dir:
                    try:
                        content = await executor_client.read_file(project_id, entry.name)
                        files[entry.name] = content
                    except Exception as exc:
                        logger.warning(
                            "project %s: failed to read '%s' for review: %s",
                            project_id, entry.name, exc,
                        )
        except Exception as exc:
            logger.warning("project %s: failed to list files for review: %s", project_id, exc)

    user_prompt = _build_user_prompt(project_id, files)

    request = CompletionRequest(
        messages=[
            CompletionMessage(role="system", content=_REVIEW_SYSTEM_PROMPT),
            CompletionMessage(role="user", content=user_prompt),
        ]
    )
    response = await provider.complete(request)

    result = _parse_review_result(response.content)
    logger.info(
        "project %s: reviewer verdict=%s, findings=%d",
        project_id, result.verdict, len(result.findings),
    )
    return result
