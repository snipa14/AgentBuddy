"""Unit tests for the Memory Curator agent.

Per .claude/rules/testing.md, LLM-backed agent tests mock the provider at
the model_router.resolve() boundary, not deeper -- router_fakes.py's
FakeRouter/FakeChatModel stand in for a real BaseChatModel.
"""

import asyncio

from services.orchestrator.agents.memory_curator import (
    CuratedGlobalMemory,
    CurationResult,
    curate_and_store_phase,
    curate_phase,
)
from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter, structured
from services.orchestrator.memory.global_store import build_global_memory_connection, recall
from services.orchestrator.memory.project_memory import read_memory_file

RESULT_WITH_MEMORY = CurationResult(
    summary_markdown="Implemented the login endpoint and fixed a flaky test.\n",
    global_memories=[
        CuratedGlobalMemory(
            type="fix",
            tags="auth flaky-test",
            content="pytest fixtures needed an explicit teardown here.",
        )
    ],
)

RESULT_NO_MEMORY = CurationResult(
    summary_markdown="Wrote the initial README, nothing cross-project relevant.\n",
    global_memories=[],
)


def test_curate_phase_returns_summary_and_global_memories():
    router = FakeRouter([structured(RESULT_WITH_MEMORY)])

    result, message = asyncio.run(curate_phase(router, "proj-1", ["did a thing", "fixed a bug"]))

    assert "Implemented the login endpoint" in result.summary_markdown
    assert len(result.global_memories) == 1
    assert result.global_memories[0].type == "fix"
    assert result.global_memories[0].tags == "auth flaky-test"
    assert message.usage_metadata is not None


def test_curate_phase_handles_response_with_no_global_memories():
    router = FakeRouter([structured(RESULT_NO_MEMORY)])

    result, _ = asyncio.run(curate_phase(router, "proj-1", ["wrote docs"]))

    assert "README" in result.summary_markdown
    assert result.global_memories == []


def test_curate_and_store_phase_updates_learnings_and_records_a_queryable_memory(tmp_path):
    router = FakeRouter([structured(RESULT_WITH_MEMORY)])
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
