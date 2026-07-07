"""Compiles the per-project subgraph (ARCHITECTURE.md §4.2, §5)."""

import functools
import sqlite3
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.nodes import (
    node_coder,
    node_cost_check,
    node_cost_wait,
    node_done,
    node_frontend_review,
    node_intake,
    node_memory_curator,
    node_merge_wait,
    node_plan_wait,
    node_planner_ask,
    node_planner_critique,
    node_planner_draft,
    node_planner_wait,
    node_reviewer,
    node_task_decomposition,
    node_testing,
    route_after_coder,
    route_after_cost_check,
    route_after_cost_wait,
    route_after_frontend_review,
    route_after_merge_wait,
    route_after_memory_curator,
    route_after_plan_wait,
    route_after_planner_ask,
    route_after_reviewer,
)
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.model_router.router import ModelRouter

DEFAULT_GLOBAL_COST_THRESHOLD_USD = 50.0


def build_project_graph(
    checkpointer: BaseCheckpointSaver,
    router: ModelRouter,
    executor_client: ExecutorClient,
    memory_conn: sqlite3.Connection,
    projects_root: Path,
    global_cost_threshold_usd: float = DEFAULT_GLOBAL_COST_THRESHOLD_USD,
) -> CompiledStateGraph:
    """Builds and compiles the per-project subgraph.

    Node chain: intake -> planner_ask <-> [planner_wait self-loop] ->
    planner_draft -> planner_critique -> plan_wait -> memory_curator ->
    task_decomposition -> coder [self-loop: next task or retry, exhausted
    retries -> memory_curator -> END] -> reviewer [changes-requested loops
    back to coder; approve advances] -> frontend_review -> cost_check ->
    [cost_wait -> memory_curator] -> testing -> memory_curator [tests failed
    -> coder; tests passed -> merge_wait] -> memory_curator -> done, with
    every human-in-the-loop gate implemented as a real `interrupt()` call
    (ADR-009 for cost/merge; the Planner's interview and final approval gate
    are Phase 8; the Coder's retry budget, Phase 9, never interrupts at all
    -- see graph/nodes.py's module docstring). `memory_curator` (Phase 14)
    sits on every phase-transition edge and approval resolution, routing
    onward based on `curator_source`/`phase` (route_after_memory_curator) --
    it is the single point every one of those paths passes through, not a
    per-transition special case.

    Args:
        checkpointer: The checkpoint saver backing thread persistence.
        router: The ModelRouter every LLM-backed node (Planner, Coder, Reviewer)
            resolves its role from.
        executor_client: The Executor client the Coder dispatches
            read_file/write_file/run_command actions through, the Reviewer uses
            to read file contents for review, and the Tester uses to run pytest
            inside a workspace container.
        memory_conn: An open global memory connection for the Memory Curator.
        projects_root: The projects/ root directory for per-project LEARNINGS.md.
        global_cost_threshold_usd: The global weekly USD threshold
            node_cost_check gates on (TODO.md Phase 18) -- summed across
            every active project's checkpointed cost_usd_weekly, not just
            this one. Defaults to DEFAULT_GLOBAL_COST_THRESHOLD_USD; a real
            deployment passes OrchestratorConfig.global_cost_threshold_usd.

    Returns:
        A compiled graph ready for `.invoke()`/`.get_state()` calls keyed by
        `{"configurable": {"thread_id": project_id}}`.
    """
    builder = StateGraph(ProjectState)
    builder.add_node("intake", node_intake)
    builder.add_node("planner_ask", functools.partial(node_planner_ask, router=router))
    builder.add_node("planner_wait", node_planner_wait)
    builder.add_node("planner_draft", functools.partial(node_planner_draft, router=router))
    builder.add_node("planner_critique", functools.partial(node_planner_critique, router=router))
    builder.add_node("plan_wait", node_plan_wait)
    builder.add_node(
        "task_decomposition", functools.partial(node_task_decomposition, router=router)
    )
    builder.add_node(
        "coder", functools.partial(node_coder, router=router, executor_client=executor_client)
    )
    builder.add_node(
        "reviewer", functools.partial(node_reviewer, router=router, executor_client=executor_client)
    )
    builder.add_node(
        "frontend_review",
        functools.partial(node_frontend_review, router=router, executor_client=executor_client),
    )
    builder.add_node(
        "cost_check",
        functools.partial(
            node_cost_check,
            checkpointer=checkpointer,
            threshold_usd=global_cost_threshold_usd,
        ),
    )
    builder.add_node("cost_wait", node_cost_wait)
    builder.add_node(
        "testing", functools.partial(node_testing, executor_client=executor_client)
    )
    builder.add_node("merge_wait", node_merge_wait)
    builder.add_node(
        "memory_curator",
        functools.partial(
            node_memory_curator,
            projects_root=projects_root,
            conn=memory_conn,
            router=router,
        ),
    )
    builder.add_node(
        "done", functools.partial(node_done, executor_client=executor_client)
    )

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "planner_ask")
    builder.add_conditional_edges(
        "planner_ask",
        route_after_planner_ask,
        {"planner_wait": "planner_wait", "planner_draft": "planner_draft"},
    )
    builder.add_edge("planner_wait", "planner_ask")
    builder.add_edge("planner_draft", "planner_critique")
    builder.add_edge("planner_critique", "plan_wait")
    builder.add_conditional_edges(
        "plan_wait",
        route_after_plan_wait,
        {"memory_curator": "memory_curator"},
    )
    builder.add_edge("task_decomposition", "coder")
    builder.add_conditional_edges(
        "coder",
        route_after_coder,
        {"coder": "coder", "reviewer": "reviewer", "memory_curator": "memory_curator"},
    )
    builder.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {"coder": "coder", "frontend_review": "frontend_review"},
    )
    builder.add_conditional_edges(
        "frontend_review",
        route_after_frontend_review,
        {"coder": "coder", "cost_check": "cost_check"},
    )
    builder.add_conditional_edges(
        "cost_check", route_after_cost_check, {"cost_wait": "cost_wait", "testing": "testing"}
    )
    builder.add_conditional_edges(
        "cost_wait", route_after_cost_wait, {"memory_curator": "memory_curator"}
    )
    builder.add_edge("testing", "memory_curator")
    builder.add_conditional_edges(
        "merge_wait", route_after_merge_wait, {"memory_curator": "memory_curator"}
    )
    builder.add_conditional_edges(
        "memory_curator",
        route_after_memory_curator,
        {
            "task_decomposition": "task_decomposition",
            "coder": "coder",
            "testing": "testing",
            "merge_wait": "merge_wait",
            "done": "done",
            END: END,
        },
    )
    builder.add_edge("done", END)

    return builder.compile(checkpointer=checkpointer)
