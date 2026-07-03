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

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from services.orchestrator.agents.llm import invoke_structured
from services.orchestrator.memory.global_store import record_memory
from services.orchestrator.memory.project_memory import append_memory_entry
from services.orchestrator.model_router.router import ModelRouter

CURATOR_ROLE = "memory_curator"

_SYSTEM_PROMPT = (
    "You curate a single software project's memory. Given a list of raw "
    "phase events, write a short markdown summary of what happened for "
    "LEARNINGS.md, and decide whether anything is worth remembering across "
    "projects -- zero or more global memories, each with a type "
    "(error_pattern, fix, preference, or skill_usage), space-separated tags, "
    "and content. Leave global_memories empty if nothing is cross-project "
    "relevant."
)


class CuratedGlobalMemory(BaseModel):
    """One global memory row proposed by the curator, before it's stored."""

    type: Literal["error_pattern", "fix", "preference", "skill_usage"]
    tags: str
    content: str


class CurationResult(BaseModel):
    """The Memory Curator's output for a single completed phase."""

    summary_markdown: str
    global_memories: list[CuratedGlobalMemory] = []


async def curate_phase(
    router: ModelRouter, project_id: str, phase_events: list[str]
) -> tuple[CurationResult, AIMessage]:
    """Summarizes a completed phase's events into markdown plus global memories.

    Args:
        router: A ModelRouter to resolve the "memory_curator" role from.
        project_id: The project the events belong to (included in the
            prompt for context only).
        phase_events: Raw event strings from the completed phase's log.

    Returns:
        A tuple of (CurationResult, raw AIMessage) -- the raw message carries
        usage_metadata for the calling node to price via ModelRouter.cost_usd().

    Raises:
        NoCapableProviderError: If no provider is configured for the
            "memory_curator" role.
        ValueError: If the model's response doesn't match CurationResult.
    """
    llm = router.resolve(CURATOR_ROLE)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=f"Project: {project_id}\nEvents:\n" + "\n".join(phase_events)),
    ]
    return await invoke_structured(llm, CurationResult, messages)


async def curate_and_store_phase(
    router: ModelRouter,
    projects_root: Path,
    conn: sqlite3.Connection,
    project_id: str,
    phase_events: list[str],
    created_at: str,
) -> tuple[CurationResult, AIMessage]:
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
        A tuple of (CurationResult, raw AIMessage) that was written to disk
        and sqlite -- the raw message carries usage_metadata for cost tracking.
    """
    result, ai_message = await curate_phase(router, project_id, phase_events)
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
    return result, ai_message
