"""Phase 14 — Memory Curator Integration tests.

Tests that the curator runs as part of the live graph and that cross-project
recall works (the Definition of Done).
"""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from langgraph.graph import END

from services.orchestrator.agents.memory_curator import CuratedGlobalMemory, CurationResult
from services.orchestrator.graph.nodes import (
    node_memory_curator,
    route_after_memory_curator,
)
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import FakeRouter, structured

# -- Fixtures -------------------------------------------------------------------

_CURATION_RESULT = CurationResult(
    summary_markdown="Completed implementation and testing for proj-cross-project.\n",
    global_memories=[
        CuratedGlobalMemory(
            type="error_pattern",
            tags="flaky-test auth",
            content=(
                "The auth fixture uses a shared session-scoped DB that causes "
                "flakes when two projects run concurrently."
            ),
        )
    ],
)


def _curator_router() -> FakeRouter:
    return FakeRouter([structured(_CURATION_RESULT)])


# -- node_memory_curator tests --------------------------------------------------


def test_node_memory_curator_skips_when_no_source():
    """No curator_source → no-op (used as pass-through)."""
    router = _curator_router()
    conn = sqlite3.connect(":memory:")
    state = new_project_state("proj-1")
    # No curator_source set

    update = node_memory_curator(state, Path("/tmp"), conn, router)

    assert update == {}


def test_node_memory_curator_skips_when_no_events():
    """Empty messages → nothing to curate."""
    router = _curator_router()
    conn = sqlite3.connect(":memory:")
    state = new_project_state("proj-1")
    state["curator_source"] = "testing"
    state["messages"] = []

    update = node_memory_curator(state, Path("/tmp"), conn, router)

    assert update == {}


def test_node_memory_curator_runs_curation_and_stores(tmp_path):
    """Curation runs, writes LEARNINGS.md, records global memory, and folds
    the completion's cost into cost_usd_session/cost_usd_weekly (ADR-026)."""
    router = _curator_router()
    from services.orchestrator.memory.global_store import build_global_memory_connection

    conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    state = new_project_state("proj-curate", initial_message="build a thing")
    state["curator_source"] = "testing"
    state["messages"] = [MagicMock(content="did implementation"), MagicMock(content="ran tests")]

    update = node_memory_curator(state, projects_root, conn, router)

    assert update["cost_usd_session"] > state["cost_usd_session"]
    assert update["cost_usd_weekly"] > state["cost_usd_weekly"]
    assert update["messages_summary"] == (
        "Completed implementation and testing for proj-cross-project.\n"
    )
    assert update["messages_curated_through"] == 2
    # Check LEARNINGS.md was written
    learnings = (projects_root / "proj-curate" / "LEARNINGS.md").read_text()
    assert "Completed implementation and testing" in learnings
    # Check global memory was recorded
    from services.orchestrator.memory.global_store import recall

    results = recall(conn, "flaky")
    assert len(results) == 1
    assert results[0].type == "error_pattern"
    conn.close()


def test_node_memory_curator_appends_to_existing_summary_rather_than_overwriting(tmp_path):
    """A second curation checkpoint's summary is appended to the first's, not
    replacing it -- messages_summary is the compacted stand-in for
    everything curated so far, not just the most recent phase."""
    router = _curator_router()
    from services.orchestrator.memory.global_store import build_global_memory_connection

    conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    state = new_project_state("proj-curate", initial_message="build a thing")
    state["curator_source"] = "testing"
    state["messages"] = [MagicMock(content="did implementation"), MagicMock(content="ran tests")]
    state["messages_summary"] = "Prior phase: planned the project."
    state["messages_curated_through"] = 0

    update = node_memory_curator(state, projects_root, conn, router)

    assert update["messages_summary"] == (
        "Prior phase: planned the project.\n\n"
        "Completed implementation and testing for proj-cross-project.\n"
    )
    assert update["messages_curated_through"] == 2
    conn.close()


def test_node_memory_curator_includes_all_events_since_cursor_not_just_last_20(tmp_path):
    """Regression test for the removed hardcoded messages[-20:] window: with
    more than 20 events accumulated since the last curation checkpoint, every
    one of them must reach the curator, not just the most recent 20."""
    router = _curator_router()
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    conn = sqlite3.connect(":memory:")

    state = new_project_state("proj-curate")
    state["curator_source"] = "testing"
    state["messages"] = [MagicMock(content=f"event {i}") for i in range(25)]
    state["messages_curated_through"] = 0

    fake_ai_message = AIMessage(
        content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    )
    with patch(
        "services.orchestrator.graph.nodes.curate_and_store_phase",
        new=AsyncMock(return_value=(_CURATION_RESULT, fake_ai_message)),
    ) as mock_curate:
        node_memory_curator(state, projects_root, conn, router)

    phase_events = mock_curate.call_args.args[4]
    assert len(phase_events) == 25


def test_node_memory_curator_still_curates_task_progress_when_no_new_messages_since_cursor(
    tmp_path,
):
    """Regression test: once messages_curated_through has caught up to
    len(messages) (nothing new since the last checkpoint), the curator must
    still fire if task_queue has content worth reporting -- task progress is
    an independent signal from new messages, not gated behind them."""
    router = _curator_router()
    from services.orchestrator.memory.global_store import build_global_memory_connection

    conn = build_global_memory_connection(tmp_path / "foundry_memory.db")
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    state = new_project_state("proj-curate", initial_message="build a thing")
    state["curator_source"] = "testing"
    state["messages_curated_through"] = len(state["messages"])
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "done", "attempts": 0}
    ]

    update = node_memory_curator(state, projects_root, conn, router)

    assert update != {}
    assert update["messages_curated_through"] == len(state["messages"])
    conn.close()


def test_node_memory_curator_skips_when_messages_never_seeded_even_with_task_progress(
    tmp_path,
):
    """Preserves the invariant test_concurrency.py depends on: a project that
    never seeds `messages` (messages stays permanently empty) must keep the
    curator silent throughout, even once task_queue has content -- task
    progress alone, without messages ever having been seeded, is not
    curation-worthy."""
    router = _curator_router()
    conn = sqlite3.connect(":memory:")
    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    state = new_project_state("proj-1")
    state["curator_source"] = "testing"
    state["messages"] = []
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "done", "attempts": 0}
    ]

    update = node_memory_curator(state, projects_root, conn, router)

    assert update == {}


def test_node_memory_curator_continues_on_failure():
    """Curator failure doesn't block the graph — logs warning and returns empty."""
    router = MagicMock()
    router.resolve.return_value = MagicMock()
    router.resolve.return_value.with_structured_output.side_effect = Exception("API error")
    conn = sqlite3.connect(":memory:")
    state = new_project_state("proj-fail", initial_message="build a thing")
    state["curator_source"] = "testing"
    state["messages"] = [MagicMock(content="event")]

    update = node_memory_curator(state, Path("/tmp"), conn, router)

    assert update == {}


# -- route_after_memory_curator tests -------------------------------------------


def test_route_from_testing_to_merge_wait_when_approved():
    """Tests passed (awaiting_merge_approval) → merge_wait."""
    state = new_project_state("proj-1")
    state["curator_source"] = "testing"
    state["phase"] = "awaiting_merge_approval"

    assert route_after_memory_curator(state) == "merge_wait"


def test_route_from_testing_to_coder_on_test_failure():
    """Tests failed (implementing) → coder."""
    state = new_project_state("proj-1")
    state["curator_source"] = "testing"
    state["phase"] = "implementing"

    assert route_after_memory_curator(state) == "coder"


def test_route_from_plan_wait_to_task_decomposition():
    """Plan approved (implementing, from plan_wait) → task_decomposition."""
    state = new_project_state("proj-1")
    state["curator_source"] = "plan_wait"
    state["phase"] = "implementing"

    assert route_after_memory_curator(state) == "task_decomposition"


def test_route_from_cost_wait_to_testing():
    """Cost approved (testing, from cost_wait) → testing."""
    state = new_project_state("proj-1")
    state["curator_source"] = "cost_wait"
    state["phase"] = "testing"

    assert route_after_memory_curator(state) == "testing"


def test_route_from_merge_wait_to_done():
    """Merge approved (done, from merge_wait) → done."""
    state = new_project_state("proj-1")
    state["curator_source"] = "merge_wait"
    state["phase"] = "done"

    assert route_after_memory_curator(state) == "done"


def test_route_from_coder_blocked_halts():
    """The Coder's exhausted-retries transition into blocked (curator_source="coder")
    also routes through curation, then halts with END."""
    state = new_project_state("proj-1")
    state["curator_source"] = "coder"
    state["phase"] = "blocked"

    assert route_after_memory_curator(state) == END


def test_route_blocked_halts():
    """Any blocked phase → END."""
    for source in ("testing", "plan_wait", "cost_wait", "merge_wait"):
        state = new_project_state("proj-1")
        state["curator_source"] = source
        state["phase"] = "blocked"

        assert route_after_memory_curator(state) == END


# -- Cross-project recall (Definition of Done) ----------------------------------


def test_cross_project_recall_second_project_sees_first_projects_fix(
    tmp_path, memory_conn, projects_root
):
    """Project B's Coder has access to Project A's recorded fix via global recall.

    This is the concrete proof the global memory layer is actually useful, not
    just present (Phase 14 Definition of Done).

    Simulates:
    1. Project A hits a flaky auth test error → curator records it as a global
       memory row (error_pattern with tags "flaky-test auth").
    2. Project B's Coder queries the global store and finds the fix from A.
    """
    from services.orchestrator.memory.global_store import recall, record_memory

    # Step 1: Record a cross-project lesson from project A.
    record_memory(
        memory_conn,
        project_id="proj-a",
        type_="error_pattern",
        content=(
            "The auth fixture uses a shared session-scoped DB that causes flakes "
            "when two projects run concurrently. Fix: use transactional rollbacks "
            "instead of truncating the table."
        ),
        tags="flaky-test auth concurrency",
        created_at="2026-07-01T00:00:00Z",
    )

    # Step 2: Project B queries global memory for relevant lessons.
    results = recall(memory_conn, "flaky auth")

    assert len(results) == 1
    assert results[0].project_id == "proj-a"
    assert results[0].type == "error_pattern"
    assert "transactional rollbacks" in results[0].content

    # Step 3: Verify the lesson is also retrievable by tag.
    tag_results = recall(memory_conn, "concurrency")
    assert len(tag_results) == 1
    assert tag_results[0].project_id == "proj-a"

    memory_conn.close()


def test_cross_project_recall_filters_by_project_when_requested(
    tmp_path, memory_conn, projects_root
):
    """recall(project_id=X) only returns memories from that project."""
    from services.orchestrator.memory.global_store import record_memory

    record_memory(
        memory_conn,
        project_id="proj-a",
        type_="fix",
        content="fixed auth bug in proj-a",
        tags="auth fix",
        created_at="2026-07-01T00:00:00Z",
    )
    record_memory(
        memory_conn,
        project_id="proj-b",
        type_="fix",
        content="fixed auth bug in proj-b",
        tags="auth fix",
        created_at="2026-07-01T00:01:00Z",
    )

    from services.orchestrator.memory.global_store import recall

    all_results = recall(memory_conn, "auth")
    assert len(all_results) == 2

    a_results = recall(memory_conn, "auth", project_id="proj-a")
    assert len(a_results) == 1
    assert a_results[0].project_id == "proj-a"

    memory_conn.close()


def test_curator_skipped_on_blocked_transition(tmp_path, memory_conn, projects_root):
    """When phase is blocked, route_after_memory_curator returns END — no curation needed."""
    state = new_project_state("proj-blocked")
    state["curator_source"] = "plan_wait"
    state["phase"] = "blocked"

    assert route_after_memory_curator(state) == END


def test_node_done_still_works_without_git_fields(tmp_path, memory_conn, projects_root):
    """node_memory_curator is a no-op when curator_source is absent, regardless
    of any git_branch/git_remote_url fields on state (those are node_done's
    concern, exercised in test_integrator.py)."""
    state = new_project_state("proj-1")
    state["phase"] = "done"
    # No curator_source set

    update = node_memory_curator(state, projects_root, memory_conn, _curator_router())

    assert update == {}
