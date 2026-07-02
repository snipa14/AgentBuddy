"""Stub nodes for the per-project subgraph (Phase 5 skeleton).

Real Planner/Coder/Reviewer/Tester agents (later phases) replace these node
bodies; Phase 5 only proves the graph shape, the two hard interrupt gates
(ADR-009), and checkpointer persistence work end to end.

Each gate is split into two nodes -- a "check" node that decides whether to
pause and, if so, commits the `phase`/`pending_approval` update, and a
"wait" node that calls `interrupt()`. This split exists because a node's
returned state update is only committed once the node function fully
returns; a node that calls `interrupt()` never returns on its first pass; so
`pending_approval` must be written by the node *before* the one that
actually interrupts, or it would never be visible to a caller inspecting
state while the graph is paused.
"""

import logging
from typing import Any

from langgraph.graph import END
from langgraph.types import interrupt

from services.orchestrator.graph.state import ProjectState

logger = logging.getLogger(__name__)

COST_THRESHOLD_USD_STUB = 50.0


def node_intake(state: ProjectState) -> dict[str, Any]:
    """Logs entry into intake and advances to planning."""
    logger.info("project %s: intake", state["project_id"])
    return {"phase": "planning"}


def node_planning(state: ProjectState) -> dict[str, Any]:
    """Logs entry into planning and advances to implementing."""
    logger.info("project %s: planning", state["project_id"])
    return {"phase": "implementing"}


def node_implementing(state: ProjectState) -> dict[str, Any]:
    """Logs entry into implementing and advances to testing."""
    logger.info("project %s: implementing", state["project_id"])
    return {"phase": "testing"}


def node_cost_check(state: ProjectState) -> dict[str, Any]:
    """Trips the cost-threshold gate if session cost is at or above the stub limit.

    Returns an empty update (no gate) unless `cost_usd_session` has reached
    `COST_THRESHOLD_USD_STUB`, in which case it commits the pending approval
    payload that `node_cost_wait` will interrupt on next.
    """
    if state["cost_usd_session"] < COST_THRESHOLD_USD_STUB:
        return {}
    logger.info("project %s: cost threshold reached", state["project_id"])
    return {
        "phase": "awaiting_cost_approval",
        "pending_approval": {
            "kind": "cost_alert",
            "project_id": state["project_id"],
            "cost_usd_session": state["cost_usd_session"],
            "threshold_usd": COST_THRESHOLD_USD_STUB,
        },
    }


def route_after_cost_check(state: ProjectState) -> str:
    """Routes to the wait node only if node_cost_check just tripped the gate."""
    if state["phase"] == "awaiting_cost_approval":
        return "cost_wait"
    return "testing"


def node_cost_wait(state: ProjectState) -> dict[str, Any]:
    """Pauses for a human decision on whether to continue past the cost gate."""
    decision = interrupt(state["pending_approval"])
    if decision != "continue":
        return {"phase": "blocked", "pending_approval": None}
    return {"phase": "testing", "pending_approval": None}


def route_after_cost_wait(state: ProjectState) -> str:
    """Routes onward to testing, or halts the run if the gate was rejected."""
    if state["phase"] == "blocked":
        return END
    return "testing"


def node_testing(state: ProjectState) -> dict[str, Any]:
    """Logs entry into testing and unconditionally arms the merge-to-main gate.

    Merge-to-main is a hard gate (ADR-009) -- it always requires approval,
    unlike the cost gate, which only fires past a threshold.
    """
    logger.info("project %s: testing", state["project_id"])
    return {
        "phase": "awaiting_merge_approval",
        "pending_approval": {"kind": "merge_approval", "project_id": state["project_id"]},
    }


def node_merge_wait(state: ProjectState) -> dict[str, Any]:
    """Pauses for a human decision on whether to merge to main."""
    decision = interrupt(state["pending_approval"])
    if decision != "approve":
        return {"phase": "blocked", "pending_approval": None}
    return {"phase": "done", "pending_approval": None}


def route_after_merge_wait(state: ProjectState) -> str:
    """Routes onward to done, or halts the run if the merge was rejected."""
    if state["phase"] == "blocked":
        return END
    return "done"


def node_done(state: ProjectState) -> dict[str, Any]:
    """Terminal node; nothing left to advance."""
    logger.info("project %s: done", state["project_id"])
    return {}
