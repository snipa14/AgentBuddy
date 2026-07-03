"""Nodes for the per-project subgraph (ARCHITECTURE.md §4.7, §6.1-6.2).

The Planner (Phase 8), Coder (Phase 9), Reviewer (Phase 10), and Tester
(Phase 11) agents all replace what used to be single stub "planning"/
"implementing"/"testing" nodes with real flows: interview -> draft -> critique
-> approval, task-decomposition -> implement -> retry, review -> approve/revise,
and pytest + coverage gate.

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

`node_coder` and `node_reviewer` are different: they never call `interrupt()`
at all (fully autonomous within their own retry budgets), so they're single
self-looping nodes with no "wait" counterpart, looping back to themselves via
`route_after_coder`/`route_after_reviewer` for either the next task or a
revision of the current one.

LLM calls happen inside `services.orchestrator.agents.planner`/`coder`/
`reviewer`, which are plain `async def` and independently testable without
LangGraph. The Tester agent (`tester.py`) makes no LLM calls — it only talks
to the Executor over HTTP. These node functions stay synchronous (matching
every other node in this module) and bridge to that async layer with
`asyncio.run()`, rather than making the whole graph async just for the nodes
that need an LLM or network call (ADR-019).
"""

import asyncio
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END
from langgraph.types import interrupt

from services.orchestrator.agents.coder import decompose_plan_into_tasks, run_coder_task
from services.orchestrator.agents.planner import (
    DraftPlan,
    apply_critique,
    ask_clarifying_questions,
    critique_plan,
    draft_plan,
)
from services.orchestrator.agents.tester import TestOutcome, _evaluate_gate, run_tests
from services.orchestrator.agents.reviewer import review_changes
from services.orchestrator.agents.frontend_reviewer import (
    _has_frontend_files,
    _build_findings_text,
    review_frontend,
)
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.model_router.router import ModelRouter

logger = logging.getLogger(__name__)

COST_THRESHOLD_USD_STUB = 50.0
MAX_TASK_ATTEMPTS = 3


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
    """Routes onward to task decomposition, or halts the run if the plan was rejected."""
    if state["phase"] == "blocked":
        return END
    return "task_decomposition"


def node_task_decomposition(state: ProjectState, router: ModelRouter) -> dict[str, Any]:
    """Breaks the approved plan into task_queue, once, right after plan approval."""
    tasks = asyncio.run(decompose_plan_into_tasks(router, state["plan"]))
    logger.info("project %s: decomposed into %d task(s)", state["project_id"], len(tasks))
    return {"task_queue": [task.model_dump() for task in tasks], "phase": "implementing"}


def node_coder(
    state: ProjectState, router: ModelRouter, executor_client: ExecutorClient
) -> dict[str, Any]:
    """Implements one task-or-retry-attempt per execution.

    No `interrupt()` call happens anywhere in this node (the Coder is fully
    autonomous within its retry budget), so -- unlike the Planner/cost/merge
    gates -- there's no LangGraph replay concern here: this node can safely
    make LLM and Executor calls directly. Retry count is tracked in
    `current_task["attempts"]` rather than a new ProjectState field, since
    `current_task` is already a free-form dict.
    """
    task_queue = state["task_queue"]
    if not task_queue:
        return {"phase": "reviewing"}

    current_task = state["current_task"] or {**task_queue[0], "attempts": 0}
    result = asyncio.run(
        run_coder_task(router, executor_client, state["project_id"], current_task["description"])
    )
    if result.success:
        remaining = task_queue[1:]
        logger.info(
            "project %s: task succeeded, %d remaining", state["project_id"], len(remaining)
        )
        return {
            "task_queue": remaining,
            "current_task": None,
            "phase": "implementing" if remaining else "reviewing",
        }

    attempts = current_task.get("attempts", 0) + 1
    if attempts >= MAX_TASK_ATTEMPTS:
        logger.warning(
            "project %s: task exhausted %d attempts, blocking", state["project_id"], attempts
        )
        return {"phase": "blocked", "current_task": {**current_task, "attempts": attempts}}
    logger.info("project %s: task attempt %d failed, retrying", state["project_id"], attempts)
    return {"current_task": {**current_task, "attempts": attempts}, "phase": "implementing"}


def route_after_coder(state: ProjectState) -> str:
    """Loops back for the next task/retry, halts if blocked, routes to reviewer when done."""
    if state["phase"] == "blocked":
        return END
    if state["phase"] == "reviewing":
        return "reviewer"
    return "coder"


def node_reviewer(state: ProjectState, router: ModelRouter, executor_client: ExecutorClient) -> dict[str, Any]:
    """Reviews all project files after the Coder finishes implementing.

    Reads every file in the workspace container via the Executor client,
    sends them to an LLM with a review prompt, and parses the structured
    verdict (approve / changes-requested). Changes-requested routes back
    to the Coder with findings attached; approve advances to cost_check.
    """
    result = asyncio.run(review_changes(router, executor_client, state["project_id"]))

    if result.verdict == "changes-requested":
        logger.info(
            "project %s: reviewer requested changes (%d finding(s))",
            state["project_id"], len(result.findings),
        )
        findings_text = "\n".join(
            f"- {f.file}:{f.line} [{f.severity}] {f.message}" for f in result.findings
        )
        return {
            "phase": "implementing",
            "messages": [AIMessage(content=f"Reviewer feedback:\n{findings_text}")],
        }

    logger.info("project %s: reviewer approved all changes", state["project_id"])
    return {"phase": "testing"}


def route_after_reviewer(state: ProjectState) -> str:
    """Routes back to Coder for revisions, or onward to frontend review."""
    if state["phase"] == "implementing":
        return "coder"
    return "frontend_review"


def node_frontend_review(
    state: ProjectState, router: ModelRouter, executor_client: ExecutorClient
) -> dict[str, Any]:
    """Takes screenshots of the running dev server and sends them to a vision-capable provider.

    Runs after code review approves changes. Skips entirely if no frontend
    files exist in the project. Otherwise captures screenshots at desktop and
    mobile breakpoints, sends them to the LLM for visual review, and attaches
    any findings to messages. On success (approve), advances to cost_check.
    On failure (changes-requested), routes back to Coder with specific UI feedback.
    """
    if not _has_frontend_files(state["project_id"], executor_client):
        logger.info("project %s: no frontend files, skipping visual review", state["project_id"])
        return {"phase": "testing"}

    result_verdict, findings = asyncio.run(
        review_frontend(router, executor_client, state["project_id"])
    )

    if result_verdict == "changes-requested" and findings:
        logger.info(
            "project %s: frontend reviewer requested changes (%d finding(s))",
            state["project_id"], len(findings),
        )
        findings_text = _build_findings_text(findings)
        return {
            "phase": "implementing",
            "messages": [AIMessage(content=findings_text)],
        }

    logger.info("project %s: frontend reviewer approved all changes", state["project_id"])
    return {"phase": "testing"}


def route_after_frontend_review(state: ProjectState) -> str:
    """Routes back to Coder for UI revisions, or onward to cost_check."""
    if state["phase"] == "implementing":
        return "coder"
    return "cost_check"


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


def node_testing(state: ProjectState, executor_client: ExecutorClient) -> dict[str, Any]:
    """Runs the test suite inside a workspace container and enforces coverage gates.

    Calls ``executor_client.run_tests()`` (pytest + coverage.py), parses results,
    and evaluates against the project's configured ``coverage_threshold`` using
    the project's configured ``coverage_gate_mode`` (independent of ``pr_mode``,
    which only governs GitHub PR automation -- ADR-023):

    - **hard** (default): any test failure or coverage below threshold routes back
      to Coder with specific failing tests / uncovered lines attached.
    - **soft**: coverage shortfall produces a warning but allows passage; test
      failures still route back to Coder.
    - **configurable**: treated as hard for now — project-level policy selection
      will control this in Phase 14+.

    On success, arms the merge-to-main gate (ADR-009). On failure, attaches
    detailed feedback to ``messages`` and returns phase "implementing" so the
    Coder can fix specific issues.
    """
    logger.info("project %s: running tests", state["project_id"])

    gate_mode = state.get("coverage_gate_mode", "hard")

    outcome = asyncio.run(run_tests(executor_client, state["project_id"], threshold=state["coverage_threshold"], gate_mode=gate_mode))

    if outcome.verdict == "fail":
        # Extract failing test / uncovered-line details for the Coder
        from services.orchestrator.agents.tester import (
            _parse_failing_tests,
            _parse_uncovered_lines,
        )

        failures = _parse_failing_tests(outcome.report)
        if failures:
            feedback_lines = ["Test failures:"] + [f"- {f.message}" for f in failures]
        else:
            # Coverage shortfall — no test failures, just low coverage
            feedback_lines = [
                f"Coverage {outcome.coverage_pct:.1f}% below threshold "
                f"{state['coverage_threshold']}% (gate mode: {gate_mode})"
            ]
            uncovered = _parse_uncovered_lines(outcome.report)
            if uncovered:
                feedback_lines.append("Uncovered lines:")
                feedback_lines.extend(f"- {entry}" for entry in uncovered)

        return {
            "phase": "implementing",
            "messages": [AIMessage(content="\n".join(feedback_lines))],
            "coverage_pct": outcome.coverage_pct,
        }

    # All tests passed and coverage meets threshold (or soft mode) — arm merge gate
    logger.info(
        "project %s: tests passed (%d/%d), coverage %.1f%%",
        state["project_id"],
        outcome.passed,
        outcome.failed,
        outcome.coverage_pct or 0.0,
    )
    return {
        "phase": "awaiting_merge_approval",
        "coverage_pct": outcome.coverage_pct,
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
