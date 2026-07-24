"""Phase 18 -- proves LangGraph's per-thread_id checkpointing (ADR-006)
doesn't bleed ProjectState between concurrently active projects.

Drives two fixture projects through the same compiled graph, interleaved,
using one shared FakeRouter whose scripted responses are ordered to match
the exact invoke() sequence below -- this test controls that sequence
directly (no true OS-thread concurrency needed), so it proves the property
actually under test here: LangGraph's thread_id-keyed state isolation, not
anything about Python-level threading (see model_router's own
test_throttled_chat_model_serializes_concurrent_ainvoke_calls for that).

Deliberately does not seed `initial_message` on either project: doing so
would give node_memory_curator's `phase_events` a non-empty `messages` list
(curator_source is set to a truthy value at both the plan_wait and testing
transitions in this cascade, so the node's operative guard is really the
"messages is empty" check, not the curator_source check its docstring leads
with) -- which would fire two additional, unscripted curator LLM calls per
project mid-cascade, exactly as test_graph_end_to_end.py's own module
docstring already warns for fast_track_router()-driven tests. Proving
plan/task_queue isolation (the concrete fields TODO.md Phase 18 calls out)
doesn't need messages seeded at all.
"""

from langgraph.types import Command

from services.orchestrator.agents.coder import CoderTask, _TaskList
from services.orchestrator.agents.planner import CritiqueResult, DraftPlan, InterviewTurn
from services.orchestrator.executor_client import CommandResult, TestResult
from services.orchestrator.graph.build import build_project_graph
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import (
    CODER_DONE,
    REVIEWER_APPROVE,
    FakeRouter,
    structured,
)
from services.orchestrator.stacks import StacksConfig


class _StubExecutorClient:
    """Reports a clean passing test run and successful git commands --
    same shape as test_graph_end_to_end.py's copy (deliberately not shared,
    per planner_fakes.py's own precedent for not coupling test fixtures
    across files)."""

    async def run_tests(self, project_id: str, framework: str = "pytest", stack: str = "python"):
        return TestResult(passed=1, failed=0, coverage_pct=100.0, report="1 passed")

    async def run_command(self, project_id: str, command, timeout=None, stack: str = "python"):
        return CommandResult(stdout="", stderr="", exit_code=0)


def _config(project_id: str) -> dict:
    return {"configurable": {"thread_id": project_id}}


def _start_script(plan_summary: str) -> list:
    """One project's Planner interview->draft->critique responses, consumed
    by the FIRST invoke() call for that project (through to plan_wait)."""
    return [
        structured(InterviewTurn(status="ready")),
        structured(DraftPlan(summary=plan_summary)),
        structured(CritiqueResult()),
    ]


def _resume_script(task_description: str) -> list:
    """One project's task-decomposition->coder->reviewer responses,
    consumed by the SECOND invoke() call for that project (resuming
    plan_wait, cascading through to merge_wait). No memory_curator
    responses needed -- messages stays empty for the whole run (no
    initial_message, and node_planner_ask's "ready" branch adds no
    messages either), so node_memory_curator's empty-phase_events guard
    fires on both cascade passes and makes no LLM call."""
    return [
        structured(_TaskList(tasks=[CoderTask(description=task_description)])),
        CODER_DONE,
        REVIEWER_APPROVE,
    ]


def test_two_interleaved_projects_do_not_bleed_plan_or_task_queue(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    # Queue order matches the actual interleaved invoke() sequence below --
    # NOT "alpha's whole run, then beta's whole run": both projects' FIRST
    # invoke() only runs to plan_wait, so both starts happen before either
    # resume.
    router = FakeRouter(
        _start_script("Alpha-only plan")
        + _start_script("Beta-only plan")
        + _resume_script("Alpha-only task")
        + _resume_script("Beta-only task")
    )
    graph = build_project_graph(
        checkpointer,
        router,
        _StubExecutorClient(),
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config_a = _config("proj-alpha")
    config_b = _config("proj-beta")

    graph.invoke(new_project_state("proj-alpha"), config=config_a)
    graph.invoke(new_project_state("proj-beta"), config=config_b)

    snapshot_a = graph.get_state(config_a)
    snapshot_b = graph.get_state(config_b)
    assert snapshot_a.values["phase"] == "awaiting_plan_approval"
    assert snapshot_b.values["phase"] == "awaiting_plan_approval"
    assert snapshot_a.values["plan"]["summary"] == "Alpha-only plan"
    assert snapshot_b.values["plan"]["summary"] == "Beta-only plan"
    assert snapshot_a.values["project_id"] == "proj-alpha"
    assert snapshot_b.values["project_id"] == "proj-beta"

    graph.invoke(Command(resume="approve"), config=config_a)
    graph.invoke(Command(resume="approve"), config=config_b)

    final_a = graph.get_state(config_a)
    final_b = graph.get_state(config_b)
    assert final_a.values["phase"] == "awaiting_merge_approval"
    assert final_b.values["phase"] == "awaiting_merge_approval"
    assert final_a.values["plan"]["summary"] == "Alpha-only plan"
    assert final_b.values["plan"]["summary"] == "Beta-only plan"
    assert final_a.values["project_id"] == "proj-alpha"
    assert final_b.values["project_id"] == "proj-beta"
