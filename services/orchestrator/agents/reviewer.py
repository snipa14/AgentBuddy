"""Reviewer agent — inspects code changes and returns a structured verdict.

Runs after the Coder finishes all tasks, before tests execute. Reads every file
in the project, sends it to an LLM with a review prompt, and gets back a
schema-forced ReviewResult (ADR-025) -- no hand-rolled markdown parsing.
Routes back to Coder if changes are requested.

Per .claude/rules/testing.md: mocks the provider at model_router.resolve(), not
deeper.
"""

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from services.orchestrator.agents.llm import invoke_structured
from services.orchestrator.model_router.router import ModelRouter

logger = logging.getLogger(__name__)

REVIEWER_ROLE = "reviewer"

_REVIEW_SYSTEM_PROMPT = (
    "You are a code reviewer. You will receive the contents of files that were "
    "just written by a Coder agent inside a workspace container.\n\n"
    "Review each file for:\n"
    "- Security issues (hardcoded secrets, injection vulnerabilities, missing null checks)\n"
    "- Logic errors (off-by-one, unhandled edge cases, incorrect assumptions)\n"
    "- Scope creep (code outside the stated plan goals)\n\n"
    "For each issue found, report the file, an approximate line number, a "
    "severity ('critical' or 'warning'), and a concise message. If there are "
    "no issues, the verdict must be 'approve' and findings must be empty.\n\n"
    "If an issue is not fixable by editing the current diff—because a task's "
    "premise is false, a dependency the plan assumed doesn't exist, or the "
    "approach is structurally wrong—set replan_needed to true with a concrete "
    "replan_reason, and still report the findings. Otherwise leave replan_needed "
    "false and replan_reason empty."
)


class ReviewFinding(BaseModel):
    """A single review finding with file, line, severity, and message."""

    file: str = Field(description="File path where the issue was found")
    line: int = Field(description="Approximate line number of the issue")
    severity: str = Field(description="'critical' or 'warning'")
    message: str = Field(description="Concise description of the issue")


class ReviewResult(BaseModel):
    """The reviewer's verdict, structured findings, and optional replan signal.

    When a finding cannot be fixed by editing the diff because the underlying plan
    task rests on a false assumption, set replan_needed to True with a concrete
    reason. Otherwise leave both replan fields at their defaults.
    """

    verdict: str = Field(
        description="'approve' if no issues; 'changes-requested' if at least one finding"
    )
    findings: list[ReviewFinding] = Field(default_factory=list)
    replan_needed: bool = Field(
        default=False,
        description=(
            "True only when a finding cannot be fixed by editing the diff "
            "because the underlying plan task rests on a false assumption"
        ),
    )
    replan_reason: str = Field(
        default="", description="Why the plan itself is wrong, when replan_needed"
    )


def _build_user_prompt(project_id: str, files: dict[str, str]) -> str:
    """Builds the user prompt with file contents for review."""
    parts = [f"Project: {project_id}\n\n"]
    for path, content in sorted(files.items()):
        parts.append(f"### {path}\n```\n{content}\n```\n")
    return "\n".join(parts)


async def review_changes(
    router: ModelRouter,
    executor_client,
    project_id: str,
    changed_files: list[str] | None = None,
) -> tuple[ReviewResult, AIMessage]:
    """Reviews code changes and returns a structured verdict.

    Reads all files in the project (or only the listed changed files if provided),
    sends them to an LLM with a review prompt, and gets back a schema-forced result.

    Args:
        router: The ModelRouter used to resolve the 'reviewer' role's provider.
        executor_client: The Executor client for reading file contents inside
            the workspace container.
        project_id: The project identifier (used as thread_id and in logging).
        changed_files: Optional list of file paths that were modified during coding.
            If None, all files in the project root are reviewed.

    Returns:
        A tuple of (ReviewResult, raw AIMessage) -- the raw message carries
        usage_metadata for the calling node to price via ModelRouter.cost_usd().

    Raises:
        ValueError: If the model's response doesn't match ReviewResult.
    """
    llm = router.resolve(REVIEWER_ROLE)

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

    messages = [
        SystemMessage(content=_REVIEW_SYSTEM_PROMPT),
        HumanMessage(content=_build_user_prompt(project_id, files)),
    ]
    result, ai_message = await invoke_structured(llm, ReviewResult, messages)

    logger.info(
        "project %s: reviewer verdict=%s, findings=%d",
        project_id, result.verdict, len(result.findings),
    )
    return result, ai_message
