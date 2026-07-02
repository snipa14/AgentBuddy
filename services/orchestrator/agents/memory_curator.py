"""Memory Curator agent (ARCHITECTURE.md §4.5, §4.7).

Runs after each completed phase, not after every message -- summarizing at
that granularity keeps LLM calls infrequent and cheap relative to the
coding/review loop. Distills a phase's raw event log into a markdown
summary for the project's `LEARNINGS.md` and zero or more rows for the
global, cross-project memory store.
"""

import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from services.orchestrator.memory.global_store import record_memory
from services.orchestrator.memory.project_memory import append_memory_entry
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.model_router.schemas import CompletionMessage, CompletionRequest

CURATOR_ROLE = "memory_curator"

_SYSTEM_PROMPT = (
    "You curate a single software project's memory. Given a list of raw "
    "phase events, write a short markdown summary of what happened for "
    "LEARNINGS.md, and decide whether anything is worth remembering across "
    "projects. Respond with exactly two sections: '## Summary' followed by "
    "the markdown summary, and '## Global Memories' followed by zero or "
    "more lines, each formatted as "
    "'type: <error_pattern|fix|preference|skill_usage> | tags: <space "
    "separated tags> | <content>'. Omit the Global Memories section "
    "entirely if nothing is cross-project relevant."
)


class CuratedGlobalMemory(BaseModel):
    """One global memory row proposed by the curator, before it's stored."""

    type: Literal["error_pattern", "fix", "preference", "skill_usage"]
    tags: str
    content: str


class CurationResult(BaseModel):
    """The Memory Curator's output for a single completed phase."""

    summary_markdown: str
    global_memories: list[CuratedGlobalMemory]


async def curate_phase(
    router: ModelRouter, project_id: str, phase_events: list[str]
) -> CurationResult:
    """Summarizes a completed phase's events into markdown plus global memories.

    Args:
        router: A ModelRouter to resolve the "memory_curator" role from.
        project_id: The project the events belong to (included in the
            prompt for context only).
        phase_events: Raw event strings from the completed phase's log.

    Returns:
        A CurationResult with a markdown summary and zero or more proposed
        global memory rows.

    Raises:
        NoCapableProviderError: If no provider is configured for the
            "memory_curator" role.
        ProviderRequestError: If the underlying model call fails.
        ValueError: If the model's response doesn't match the expected format.
    """
    provider = router.resolve(CURATOR_ROLE)
    request = CompletionRequest(
        messages=[
            CompletionMessage(role="system", content=_SYSTEM_PROMPT),
            CompletionMessage(
                role="user",
                content=f"Project: {project_id}\nEvents:\n" + "\n".join(phase_events),
            ),
        ]
    )
    result = await provider.complete(request)
    return _parse_curation_response(result.content)


async def curate_and_store_phase(
    router: ModelRouter,
    projects_root: Path,
    conn: sqlite3.Connection,
    project_id: str,
    phase_events: list[str],
    created_at: str,
) -> CurationResult:
    """Curates a completed phase and writes the result to both memory layers.

    Args:
        router: A ModelRouter to resolve the "memory_curator" role from.
        projects_root: The projects/ root directory, for the per-project
            LEARNINGS.md file.
        conn: An open global memory connection from build_global_memory_connection().
        project_id: The project the phase belongs to.
        phase_events: Raw event strings from the completed phase's log.
        created_at: An ISO-8601 timestamp applied to every global memory row
            created from this phase.

    Returns:
        The CurationResult that was written to disk and sqlite.
    """
    result = await curate_phase(router, project_id, phase_events)
    append_memory_entry(projects_root, project_id, "LEARNINGS.md", result.summary_markdown)
    for memory in result.global_memories:
        record_memory(
            conn,
            project_id=project_id,
            type_=memory.type,
            content=memory.content,
            tags=memory.tags,
            created_at=created_at,
        )
    return result


def _parse_curation_response(content: str) -> CurationResult:
    """Parses the curator's '## Summary' / '## Global Memories' response text."""
    summary_lines: list[str] = []
    memory_lines: list[str] = []
    section: str | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## Summary":
            section = "summary"
            continue
        if stripped == "## Global Memories":
            section = "memories"
            continue
        if section == "summary":
            summary_lines.append(line)
        elif section == "memories" and stripped:
            memory_lines.append(stripped)

    return CurationResult(
        summary_markdown="\n".join(summary_lines).strip() + "\n",
        global_memories=[_parse_memory_line(line) for line in memory_lines],
    )


def _parse_memory_line(line: str) -> CuratedGlobalMemory:
    """Parses one 'type: X | tags: Y | content' line into a CuratedGlobalMemory.

    Raises:
        ValueError: If the line doesn't match the expected pipe-delimited
            format, or `type` isn't one of the four known memory types.
    """
    parts = [part.strip() for part in line.split("|", maxsplit=2)]
    if len(parts) != 3 or not parts[0].startswith("type:") or not parts[1].startswith("tags:"):
        raise ValueError(f"malformed global memory line: {line!r}")
    return CuratedGlobalMemory(
        type=parts[0].removeprefix("type:").strip(),
        tags=parts[1].removeprefix("tags:").strip(),
        content=parts[2].strip(),
    )
