"""Compiles the per-project subgraph (ARCHITECTURE.md §4.2, §5)."""

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
    node_planning,
    node_testing,
    route_after_cost_check,
    route_after_cost_wait,
    route_after_merge_wait,
)
from services.orchestrator.graph.state import ProjectState


def build_project_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """Builds and compiles the per-project subgraph.

    Node chain: intake -> planning -> implementing -> cost_check
    -> [cost_wait] -> testing -> merge_wait -> done, with the two hard
    interrupt gates (cost threshold, merge-to-main) implemented as real
    `interrupt()` calls (ADR-009).

    Args:
        checkpointer: The checkpoint saver backing thread persistence.

    Returns:
        A compiled graph ready for `.invoke()`/`.get_state()` calls keyed by
        `{"configurable": {"thread_id": project_id}}`.
    """
    builder = StateGraph(ProjectState)
    builder.add_node("intake", node_intake)
    builder.add_node("planning", node_planning)
    builder.add_node("implementing", node_implementing)
    builder.add_node("cost_check", node_cost_check)
    builder.add_node("cost_wait", node_cost_wait)
    builder.add_node("testing", node_testing)
    builder.add_node("merge_wait", node_merge_wait)
    builder.add_node("done", node_done)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "planning")
    builder.add_edge("planning", "implementing")
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
