"""Nodes for the per-project subgraph (ARCHITECTURE.md §4.7, §6.1-6.2).

Coder/Reviewer/Tester agents (later phases) still replace the
`node_implementing` stub; the Planner (Phase 8) already replaces what used
to be a single stub "planning" node with a real interview -> draft ->
critique -> approval flow.

Every human-in-the-loop gate is split into two nodes -- a "check"/producer
node that decides what to show the human and commits the `phase`/
`pending_approval` update via a normal return, and a "wait" node that does
nothing but call `interrupt()`. This split exists for two reasons: (1) a
node's returned state update is only committed once the node function fully
returns, and a node that calls `interrupt()` never returns on its first
pass, so `pending_approval` must be written by a node *before* the one that
interrupts, or it would never be visible to a caller inspecting state while
the graph is paused; (2) LangGraph re-runs a node's entire body from the top
on every resume, replaying earlier `interrupt()` calls but re-executing any
other code -- so an LLM call placed before an `interrupt()` in the *same*
node would silently re-run (and could return a different answer) on every
resume. Keeping each interrupt as the sole statement in its own node avoids
that entirely.

The Planner's clarifying-question interview applies the same split as a
self-loop: `node_planner_ask` (one LLM call, no interrupt) decides whether
to ask another batch of questions or move on, `node_planner_wait`
(interrupt only) collects the human's answer, and an unconditional edge
sends control back to `node_planner_ask` for the next round -- each round
is its own separately-checkpointed node execution, so no LLM call is ever
replayed.

LLM calls happen inside `services.orchestrator.agents.planner`, which is
plain `async def` and independently testable without LangGraph. These node
functions stay synchronous (matching every other node in this module) and
bridge to that async layer with `asyncio.run()`, rather than making the
whole graph async just for the nodes that need an LLM.
"""

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END
from langgraph.types import interrupt

from services.orchestrator.agents.planner import (
    DraftPlan,
    apply_critique,
    ask_clarifying_questions,
    critique_plan,
    draft_plan,
)
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.model_router.router import ModelRouter

logger = logging.getLogger(__name__)

COST_THRESHOLD_USD_STUB = 50.0


def node_intake(state: ProjectState) -> dict[str, Any]:
    """Logs entry into intake and advances to the Planner's interview."""
    logger.info("project %s: intake", state["project_id"])
    return {"phase": "planning"}


def _extract_description_and_history(state: ProjectState) -> tuple[str, list[str]]:
    """Splits `messages` into the original project request and the interview transcript."""
    messages = state["messages"]
    if not messages:
        return "", []
    description = messages[0].content
    qa_history = [message.content for message in messages[1:]]
    return description, qa_history


def node_planner_ask(state: ProjectState, router: ModelRouter) -> dict[str, Any]:
    """Runs one round of the clarifying-question interview.

    Either commits a fresh batch of questions to `pending_approval` (for
    `node_planner_wait` to interrupt on) or, once the Planner signals it has
    enough information, advances to the "roasting" phase.
    """
    description, qa_history = _extract_description_and_history(state)
    turn = asyncio.run(ask_clarifying_questions(router, description, qa_history))
    if turn.status == "ready":
        logger.info("project %s: interview complete", state["project_id"])
        return {"phase": "roasting", "pending_approval": None}

    questions_text = "\n".join(f"- {question}" for question in turn.questions)
    return {
        "phase": "planning",
        "messages": [AIMessage(content=questions_text)],
        "pending_approval": {
            "kind": "plan_questions",
            "thread_id": state["project_id"],
            "project_id": state["project_id"],
            "questions": turn.questions,
            "summary": questions_text,
        },
    }


def route_after_planner_ask(state: ProjectState) -> str:
    """Routes to the wait node for another round, or onward once ready to draft."""
    if state["phase"] == "roasting":
        return "planner_draft"
    return "planner_wait"


def node_planner_wait(state: ProjectState) -> dict[str, Any]:
    """Pauses for the stakeholder's answer to the current batch of questions."""
    answer = interrupt(state["pending_approval"])
    return {
        "phase": "planning",
        "messages": [HumanMessage(content=answer)],
        "pending_approval": None,
    }


def node_planner_draft(state: ProjectState, router: ModelRouter) -> dict[str, Any]:
    """Drafts a project plan from the completed interview transcript."""
    description, qa_history = _extract_description_and_history(state)
    plan = asyncio.run(draft_plan(router, description, qa_history))
    logger.info("project %s: plan drafted", state["project_id"])
    return {"plan": plan.model_dump(), "phase": "roasting"}


def node_planner_critique(state: ProjectState, router: ModelRouter) -> dict[str, Any]:
    """Runs the roast-the-plan critique pass and arms the plan-approval gate."""
    draft = DraftPlan(**state["plan"])
    critique = asyncio.run(critique_plan(router, draft))
    final_plan = apply_critique(draft, critique)
    logger.info(
        "project %s: critique found %d finding(s)", state["project_id"], len(critique.findings)
    )
    return {
        "plan": final_plan.model_dump(),
        "phase": "awaiting_plan_approval",
        "pending_approval": {
            "kind": "plan_approval",
            "thread_id": state["project_id"],
            "project_id": state["project_id"],
            "plan": final_plan.model_dump(),
            "critique_findings": [finding.model_dump() for finding in critique.findings],
            "summary": final_plan.summary,
        },
    }


def node_plan_wait(state: ProjectState) -> dict[str, Any]:
    """Pauses for a human decision on the final, critiqued plan."""
    decision = interrupt(state["pending_approval"])
    if decision != "approve":
        return {"phase": "blocked", "pending_approval": None}
    return {"phase": "implementing", "pending_approval": None}


def route_after_plan_wait(state: ProjectState) -> str:
    """Routes onward to implementing, or halts the run if the plan was rejected."""
    if state["phase"] == "blocked":
        return END
    return "implementing"


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
            "thread_id": state["project_id"],
            "project_id": state["project_id"],
            "cost_usd_session": state["cost_usd_session"],
            "threshold_usd": COST_THRESHOLD_USD_STUB,
            "summary": (
                f"Crossed ${state['cost_usd_session']:.2f} of "
                f"${COST_THRESHOLD_USD_STUB:.2f} session cost limit -- continue?"
            ),
        },
    }


def route_after_cost_check(state: ProjectState) -> str:
    """Routes to the wait node only if node_cost_check just tripped the gate."""
    if state["phase"] == "awaiting_cost_approval":
        return "cost_wait"
    return "testing"


def node_cost_wait(state: ProjectState) -> dict[str, Any]:
    """Pauses for a human decision on whether to continue past the cost gate.

    Decision vocabulary is "approve"/"reject"/"edit", matching the Telegram
    Gateway's single generic Approve/Reject/Edit renderer (services/
    telegram_gateway/rendering.py) rather than gate-specific button text --
    every interrupt gate in this graph shares that one rendering path.
    """
    decision = interrupt(state["pending_approval"])
    if decision != "approve":
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
        "pending_approval": {
            "kind": "merge_approval",
            "thread_id": state["project_id"],
            "project_id": state["project_id"],
            "summary": f"Project {state['project_id']}: tests passed -- merge to main?",
        },
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
