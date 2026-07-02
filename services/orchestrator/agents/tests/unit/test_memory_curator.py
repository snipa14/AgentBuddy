"""Unit tests for the Memory Curator agent.

Per .claude/rules/testing.md, LLM-backed agent tests mock the provider at
the model_router.resolve() boundary, not deeper -- these tests fake a whole
Provider/ModelRouter pair rather than reaching into HTTP clients or a real
Anthropic/Ollama backend.
"""

import asyncio

import pytest

from services.orchestrator.agents.memory_curator import (
    curate_and_store_phase,
    curate_phase,
)
from services.orchestrator.memory.global_store import build_global_memory_connection, recall
from services.orchestrator.memory.project_memory import read_memory_file
from services.orchestrator.model_router.schemas import CompletionResult

CANNED_RESPONSE_WITH_MEMORY = """## Summary
Implemented the login endpoint and fixed a flaky test.

## Global Memories
type: fix | tags: auth flaky-test | pytest fixtures needed an explicit teardown here.
"""

CANNED_RESPONSE_NO_MEMORY = """## Summary
Wrote the initial README, nothing cross-project relevant.
"""


class _FakeProvider:
    """Stands in for a real Provider, returning a fixed completion."""

    name = "fake"

    def __init__(self, content: str):
        self._content = content

    def supports(self, capability):
        return True

    async def complete(self, request):
        return CompletionResult(content=self._content, model="fake-model", provider_name="fake")


class _FakeRouter:
    """Stands in for a real ModelRouter, always resolving to the same fake provider."""

    def __init__(self, content: str):
        self._provider = _FakeProvider(content)

    def resolve(self, role):
        return self._provider


def test_curate_phase_parses_summary_and_global_memories():
    router = _FakeRouter(CANNED_RESPONSE_WITH_MEMORY)

    result = asyncio.run(curate_phase(router, "proj-1", ["did a thing", "fixed a bug"]))

    assert "Implemented the login endpoint" in result.summary_markdown
    assert len(result.global_memories) == 1
    assert result.global_memories[0].type == "fix"
    assert result.global_memories[0].tags == "auth flaky-test"


def test_curate_phase_handles_response_with_no_global_memories_section():
    router = _FakeRouter(CANNED_RESPONSE_NO_MEMORY)

    result = asyncio.run(curate_phase(router, "proj-1", ["wrote docs"]))

    assert "README" in result.summary_markdown
    assert result.global_memories == []


def test_curate_phase_raises_on_malformed_memory_line():
    router = _FakeRouter("## Summary\nsomething\n\n## Global Memories\nnot the right format\n")

    with pytest.raises(ValueError, match="malformed global memory line"):
        asyncio.run(curate_phase(router, "proj-1", ["event"]))


def test_curate_and_store_phase_updates_learnings_and_records_a_queryable_memory(tmp_path):
    router = _FakeRouter(CANNED_RESPONSE_WITH_MEMORY)
    conn = build_global_memory_connection(tmp_path / "foundry_memory.db")

    asyncio.run(
        curate_and_store_phase(
            router,
            tmp_path,
            conn,
            "proj-1",
            ["implemented login", "fixed a flaky test"],
            created_at="2026-07-01T00:00:00Z",
        )
    )

    learnings = read_memory_file(tmp_path, "proj-1", "LEARNINGS.md")
    assert "Implemented the login endpoint" in learnings

    results = recall(conn, "flaky")
    assert len(results) == 1
    assert results[0].project_id == "proj-1"
    assert results[0].type == "fix"
    conn.close()
