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
    node_replan_wait,
    node_reviewer,
    node_static_checks,
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
    route_after_replan_wait,
    route_after_reviewer,
    route_after_static_checks,
)
from services.orchestrator.graph.plugin_node import node_plugin, route_after_plugin
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.plugins.discovery import LoadedPlugin
from services.orchestrator.stacks import StacksConfig

DEFAULT_GLOBAL_COST_THRESHOLD_USD = 50.0


def _plugin_node_name(manifest) -> str:
    return f"plugin_{manifest.role}"


def _wire_plugin_slot(
    builder: StateGraph,
    plugins: list[LoadedPlugin],
    router: ModelRouter,
    executor_client: ExecutorClient,
    exit_node: str,
) -> str:
    """Adds one node per plugin, chained in order and ending at exit_node.

    Args:
        builder: The graph builder being constructed.
        plugins: This slot's plugins, in the order they should run.
        router: Threaded through to every plugin node.
        executor_client: Threaded through to every plugin node.
        exit_node: The slot's real destination once every plugin in the
            chain has passed (or if the chain is empty).

    Returns:
        The entry node name the slot's fixed predecessor edge should route
        to -- exit_node itself if plugins is empty, degrading to a pure
        passthrough exactly like static_checks with nothing to check.
    """
    if not plugins:
        return exit_node

    chain_targets = [_plugin_node_name(p.manifest) for p in plugins] + [exit_node]
    for position, plugin in enumerate(plugins):
        node_name = chain_targets[position]
        next_node = chain_targets[position + 1]
        builder.add_node(
            node_name,
            functools.partial(
                node_plugin,
                router=router,
                executor_client=executor_client,
                manifest=plugin.manifest,
                skill_body=plugin.skill_body,
                next_node=next_node,
            ),
        )
        builder.add_conditional_edges(
            node_name,
            functools.partial(route_after_plugin, next_node=next_node),
            {"coder": "coder", "memory_curator": "memory_curator", next_node: next_node},
        )
    return chain_targets[0]


def build_project_graph(
    checkpointer: BaseCheckpointSaver,
    router: ModelRouter,
    executor_client: ExecutorClient,
    memory_conn: sqlite3.Connection,
    projects_root: Path,
    stacks_config: StacksConfig,
    plugins: list[LoadedPlugin] | None = None,
    global_cost_threshold_usd: float = DEFAULT_GLOBAL_COST_THRESHOLD_USD,
) -> CompiledStateGraph:
    """Builds and compiles the per-project subgraph.

    Node chain: intake -> planner_ask <-> [planner_wait self-loop] ->
    planner_draft -> planner_critique -> plan_wait -> memory_curator ->
    task_decomposition -> coder [self-loop: next task or retry, exhausted
    retries -> memory_curator -> END] -> static_checks [failure loops back
    to coder, exhausted retries -> memory_curator -> END] -> reviewer
    [changes-requested loops back to coder; approve advances] ->
    frontend_review -> cost_check -> [cost_wait -> memory_curator] ->
    testing -> memory_curator [tests failed -> coder; tests passed ->
    merge_wait] -> memory_curator -> done, with every human-in-the-loop gate
    implemented as a real `interrupt()` call (ADR-009 for cost/merge; the
    Planner's interview and final approval gate are Phase 8; the Coder's
    retry budget (Phase 9) and the static_checks gate (ADR-046) never
    interrupt at all -- see graph/nodes.py's module docstring). When the
    Coder exhausts a task's retry budget or the Reviewer decides the plan
    itself is wrong (ADR-047), both route through memory_curator to
    replan_wait -- the replan gate's own producer/wait pair -- which on
    approval sends control
    back to planner_draft for a redraft that goes through the normal
    critique and plan-approval gates again, rather than re-entering the
    interview. `memory_curator` (Phase 14) sits on every phase-transition
    edge and approval resolution, routing onward based on
    `curator_source`/`phase` (route_after_memory_curator) -- it is the
    single point every one of those paths passes through, not a
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
        stacks_config: The loaded stack registry (ADR-045) static_checks runs
            its deterministic commands from. An empty registry (no stacks
            declared) means every stack's check_commands resolve to nothing
            to run, so static_checks always passes through to the Reviewer
            unchanged -- the node degrades to a no-op rather than blocking.
        plugins: Discovered plugins (services/orchestrator/plugins/discovery.py),
            already validated and role-collision-filtered. None or an empty
            list wires the graph exactly as before this feature existed.
        global_cost_threshold_usd: The global weekly USD threshold
            node_cost_check gates on (TODO.md Phase 18) -- summed across
            every active project's checkpointed cost_usd_weekly, not just
            this one. Defaults to DEFAULT_GLOBAL_COST_THRESHOLD_USD; a real
            deployment passes OrchestratorConfig.global_cost_threshold_usd.

    Returns:
        A compiled graph ready for `.invoke()`/`.get_state()` calls keyed by
        `{"configurable": {"thread_id": project_id}}`.
    """
    plugins = plugins or []
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
        "coder",
        functools.partial(
            node_coder, router=router, executor_client=executor_client, memory_conn=memory_conn
        ),
    )
    builder.add_node(
        "static_checks",
        functools.partial(
            node_static_checks,
            executor_client=executor_client,
            stacks_config=stacks_config,
        ),
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
    builder.add_node("replan_wait", node_replan_wait)
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

    plugins_by_slot: dict[str, list[LoadedPlugin]] = {
        "after_coder": [], "after_reviewer": [], "after_tester": [],
    }
    for plugin in plugins:
        plugins_by_slot[plugin.manifest.slot].append(plugin)

    after_coder_entry = _wire_plugin_slot(
        builder, plugins_by_slot["after_coder"], router, executor_client, "reviewer"
    )
    after_reviewer_entry = _wire_plugin_slot(
        builder, plugins_by_slot["after_reviewer"], router, executor_client, "frontend_review"
    )
    after_tester_entry = _wire_plugin_slot(
        builder, plugins_by_slot["after_tester"], router, executor_client, "memory_curator"
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
        {
            "coder": "coder",
            "static_checks": "static_checks",
            "memory_curator": "memory_curator",
        },
    )
    builder.add_conditional_edges(
        "static_checks",
        route_after_static_checks,
        {
            "coder": "coder",
            "reviewer": after_coder_entry,
            "memory_curator": "memory_curator",
        },
    )
    builder.add_conditional_edges(
        "reviewer",
        route_after_reviewer,
        {
            "coder": "coder",
            "frontend_review": after_reviewer_entry,
            "memory_curator": "memory_curator",
        },
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
    builder.add_edge("testing", after_tester_entry)
    builder.add_conditional_edges(
        "merge_wait", route_after_merge_wait, {"memory_curator": "memory_curator"}
    )
    builder.add_conditional_edges(
        "replan_wait", route_after_replan_wait, {"memory_curator": "memory_curator"}
    )
    builder.add_conditional_edges(
        "memory_curator",
        route_after_memory_curator,
        {
            "task_decomposition": "task_decomposition",
            "coder": "coder",
            "testing": "testing",
            "merge_wait": "merge_wait",
            "replan_wait": "replan_wait",
            "planner_draft": "planner_draft",
            "done": "done",
            END: END,
        },
    )
    builder.add_edge("done", END)

    return builder.compile(checkpointer=checkpointer)
