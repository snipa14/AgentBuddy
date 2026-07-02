"""Compiles the per-project subgraph (ARCHITECTURE.md §4.2, §5)."""

import functools

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from services.orchestrator.graph.nodes import (
    node_cost_check,
    node_cost_wait,
    node_done,
    node_implementing,
    node_intake,
    node_merge_wait,
    node_plan_wait,
    node_planner_ask,
    node_planner_critique,
    node_planner_draft,
    node_planner_wait,
    node_testing,
    route_after_cost_check,
    route_after_cost_wait,
    route_after_merge_wait,
    route_after_plan_wait,
    route_after_planner_ask,
)
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.model_router.router import ModelRouter


def build_project_graph(
    checkpointer: BaseCheckpointSaver, router: ModelRouter
) -> CompiledStateGraph:
    """Builds and compiles the per-project subgraph.

    Node chain: intake -> planner_ask <-> [planner_wait self-loop] ->
    planner_draft -> planner_critique -> plan_wait -> implementing ->
    cost_check -> [cost_wait] -> testing -> merge_wait -> done, with every
    human-in-the-loop gate implemented as a real `interrupt()` call
    (ADR-009 for cost/merge; the Planner's interview and final approval gate
    are Phase 8).

    Args:
        checkpointer: The checkpoint saver backing thread persistence.
        router: The ModelRouter every LLM-backed node (currently only the
            Planner) resolves its role from.

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
    builder.add_node("implementing", node_implementing)
    builder.add_node("cost_check", node_cost_check)
    builder.add_node("cost_wait", node_cost_wait)
    builder.add_node("testing", node_testing)
    builder.add_node("merge_wait", node_merge_wait)
    builder.add_node("done", node_done)

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
        "plan_wait", route_after_plan_wait, {"implementing": "implementing", END: END}
    )
    builder.add_edge("implementing", "cost_check")
    builder.add_conditional_edges(
        "cost_check", route_after_cost_check, {"cost_wait": "cost_wait", "testing": "testing"}
    )
    builder.add_conditional_edges(
        "cost_wait", route_after_cost_wait, {"testing": "testing", END: END}
    )
    builder.add_edge("testing", "merge_wait")
    builder.add_conditional_edges(
        "merge_wait", route_after_merge_wait, {"done": "done", END: END}
    )
    builder.add_edge("done", END)

    return builder.compile(checkpointer=checkpointer)
