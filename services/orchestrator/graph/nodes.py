"""Nodes for the per-project subgraph (ARCHITECTURE.md §4.7, §6.1-6.2).

The Planner (Phase 8), Coder (Phase 9), Reviewer (Phase 10), and Tester
(Phase 11) agents all replace what used to be single stub "planning"/
"implementing"/"testing" nodes with real flows: interview -> draft -> critique
-> approval, task-decomposition -> implement -> retry, review -> approve/revise,
and pytest + coverage gate. The Git/GitHub Integrator (Phase 13) turns the
terminal "done" node into a real commit/push/PR step, and the Memory Curator
(Phase 14) runs after every phase-transition edge and approval resolution via
its own `memory_curator` node rather than being called inline from each of
those nodes -- see `node_memory_curator`'s docstring for why.

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
`reviewer`/`frontend_reviewer`/`memory_curator`, which are plain `async def`
functions built on `ModelRouter.resolve()`-returned `BaseChatModel`s
(ADR-025) and independently testable without LangGraph. Every one of them
returns its parsed result alongside the raw `AIMessage` so the calling node
can price the call via `ModelRouter.cost_usd()` and fold the delta into
`cost_usd_session`/`cost_usd_weekly` (ADR-026) -- `cost_usd_session`/
`cost_usd_weekly` are plain floats, not LangGraph reducer-annotated fields,
so every LLM-backed node must read the current total and add its own delta
rather than relying on automatic accumulation. The Tester agent
(`tester.py`) makes no LLM calls at all — it only talks to the Executor over
HTTP, so `node_testing` never touches cost tracking. These node functions
stay synchronous (matching every other node in this module) and bridge to
the async agent layer with `asyncio.run()`, rather than making the whole
graph async just for the nodes that need an LLM or network call (ADR-019).
"""

import asyncio
import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END
from langgraph.types import interrupt

from services.orchestrator.agents.coder import CODER_ROLE, decompose_plan_into_tasks, run_coder_task
from services.orchestrator.agents.frontend_reviewer import (
    FRONTEND_REVIEWER_ROLE,
    _build_findings_text,
    _has_frontend_files,
    review_frontend,
)
from services.orchestrator.agents.memory_curator import CURATOR_ROLE, curate_and_store_phase
from services.orchestrator.agents.planner import (
    PLANNER_ROLE,
    DraftPlan,
    apply_critique,
    ask_clarifying_questions,
    critique_plan,
    draft_plan,
)
from services.orchestrator.agents.reviewer import REVIEWER_ROLE, review_changes
from services.orchestrator.agents.tester import run_tests
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.integrator_client import IntegratorClient
from services.orchestrator.model_router.router import ModelRouter

logger = logging.getLogger(__name__)

COST_THRESHOLD_USD_STUB = 50.0
MAX_TASK_ATTEMPTS = 3


def _with_cost(state: ProjectState, update: dict[str, Any], cost_usd: float) -> dict[str, Any]:
    """Folds an LLM call's cost delta into a node's returned state update.

    `cost_usd_session`/`cost_usd_weekly` are plain floats on ProjectState,
    not reducer-annotated fields, so every LLM-backed node must read the
    current total and add its own delta rather than relying on automatic
    accumulation (ADR-026).
    """
    if cost_usd:
        update["cost_usd_session"] = state["cost_usd_session"] + cost_usd
        update["cost_usd_weekly"] = state["cost_usd_weekly"] + cost_usd
    return update


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
    turn, ai_message = asyncio.run(ask_clarifying_questions(router, description, qa_history))
    cost = router.cost_usd(PLANNER_ROLE, ai_message.usage_metadata)

    if turn.status == "ready":
        logger.info("project %s: interview complete", state["project_id"])
        return _with_cost(state, {"phase": "roasting", "pending_approval": None}, cost)

    questions_text = "\n".join(f"- {question}" for question in turn.questions)
    return _with_cost(
        state,
        {
            "phase": "planning",
            "messages": [AIMessage(content=questions_text)],
            "pending_approval": {
                "kind": "plan_questions",
                "thread_id": state["project_id"],
                "project_id": state["project_id"],
                "questions": turn.questions,
                "summary": questions_text,
            },
        },
        cost,
    )


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
    plan, ai_message = asyncio.run(draft_plan(router, description, qa_history))
    cost = router.cost_usd(PLANNER_ROLE, ai_message.usage_metadata)
    logger.info("project %s: plan drafted", state["project_id"])
    return _with_cost(state, {"plan": plan.model_dump(), "phase": "roasting"}, cost)


def node_planner_critique(state: ProjectState, router: ModelRouter) -> dict[str, Any]:
    """Runs the roast-the-plan critique pass and arms the plan-approval gate."""
    draft = DraftPlan(**state["plan"])
    critique, ai_message = asyncio.run(critique_plan(router, draft))
    cost = router.cost_usd(PLANNER_ROLE, ai_message.usage_metadata)
    final_plan = apply_critique(draft, critique)
    logger.info(
        "project %s: critique found %d finding(s)", state["project_id"], len(critique.findings)
    )
    return _with_cost(
        state,
        {
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
        },
        cost,
    )


def node_plan_wait(state: ProjectState) -> dict[str, Any]:
    """Pauses for a human decision on the final, critiqued plan."""
    decision = interrupt(state["pending_approval"])
    if decision != "approve":
        return {"phase": "blocked", "curator_source": "plan_wait", "pending_approval": None}
    return {
        "phase": "implementing",
        "curator_source": "plan_wait",
        "pending_approval": None,
    }


def route_after_plan_wait(state: ProjectState) -> str:
    """Always routes through memory_curator -- both approval and rejection are
    curator-worthy transitions (Phase 14: "any transition into blocked" plus
    "every approval resolution"). `route_after_memory_curator` is the one
    that actually halts the run with END when the plan was rejected.
    """
    return "memory_curator"


def node_task_decomposition(state: ProjectState, router: ModelRouter) -> dict[str, Any]:
    """Breaks the approved plan into task_queue, once, right after plan approval."""
    tasks, ai_message = asyncio.run(decompose_plan_into_tasks(router, state["plan"]))
    cost = router.cost_usd(CODER_ROLE, ai_message.usage_metadata)
    logger.info("project %s: decomposed into %d task(s)", state["project_id"], len(tasks))
    return _with_cost(
        state,
        {"task_queue": [task.model_dump() for task in tasks], "phase": "implementing"},
        cost,
    )


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
        run_coder_task(
            router,
            executor_client,
            state["project_id"],
            current_task["description"],
            stack=state["stack"],
        )
    )
    if result.success:
        remaining = task_queue[1:]
        logger.info(
            "project %s: task succeeded, %d remaining", state["project_id"], len(remaining)
        )
        return _with_cost(
            state,
            {
                "task_queue": remaining,
                "current_task": None,
                "phase": "implementing" if remaining else "reviewing",
            },
            result.cost_usd,
        )

    attempts = current_task.get("attempts", 0) + 1
    if attempts >= MAX_TASK_ATTEMPTS:
        logger.warning(
            "project %s: task exhausted %d attempts, blocking", state["project_id"], attempts
        )
        return _with_cost(
            state,
            {
                "phase": "blocked",
                "curator_source": "coder",
                "current_task": {**current_task, "attempts": attempts},
            },
            result.cost_usd,
        )
    logger.info("project %s: task attempt %d failed, retrying", state["project_id"], attempts)
    return _with_cost(
        state,
        {"current_task": {**current_task, "attempts": attempts}, "phase": "implementing"},
        result.cost_usd,
    )


def route_after_coder(state: ProjectState) -> str:
    """Loops back for the next task/retry, curates then halts if blocked, routes
    to reviewer when done."""
    if state["phase"] == "blocked":
        return "memory_curator"
    if state["phase"] == "reviewing":
        return "reviewer"
    return "coder"


def node_reviewer(state: ProjectState, router: ModelRouter, executor_client: ExecutorClient) -> dict[str, Any]:
    """Reviews all project files after the Coder finishes implementing.

    Reads every file in the workspace container via the Executor client,
    sends them to an LLM with a review prompt, and gets back a schema-forced
    verdict (approve / changes-requested). Changes-requested routes back
    to the Coder with findings attached; approve advances to frontend_review.
    """
    result, ai_message = asyncio.run(review_changes(router, executor_client, state["project_id"]))
    cost = router.cost_usd(REVIEWER_ROLE, ai_message.usage_metadata)

    if result.verdict == "changes-requested":
        logger.info(
            "project %s: reviewer requested changes (%d finding(s))",
            state["project_id"], len(result.findings),
        )
        findings_text = "\n".join(
            f"- {f.file}:{f.line} [{f.severity}] {f.message}" for f in result.findings
        )
        return _with_cost(
            state,
            {
                "phase": "implementing",
                "messages": [AIMessage(content=f"Reviewer feedback:\n{findings_text}")],
            },
            cost,
        )

    logger.info("project %s: reviewer approved all changes", state["project_id"])
    return _with_cost(state, {"phase": "testing"}, cost)


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
    if not _has_frontend_files(state["project_id"], executor_client, stack=state["stack"]):
        logger.info("project %s: no frontend files, skipping visual review", state["project_id"])
        return {"phase": "testing"}

    result_verdict, findings, ai_message = asyncio.run(
        review_frontend(router, executor_client, state["project_id"], stack=state["stack"])
    )
    cost = router.cost_usd(FRONTEND_REVIEWER_ROLE, ai_message.usage_metadata) if ai_message else 0.0

    if result_verdict == "changes-requested" and findings:
        logger.info(
            "project %s: frontend reviewer requested changes (%d finding(s))",
            state["project_id"], len(findings),
        )
        findings_text = _build_findings_text(findings)
        return _with_cost(
            state,
            {
                "phase": "implementing",
                "messages": [AIMessage(content=findings_text)],
            },
            cost,
        )

    logger.info("project %s: frontend reviewer approved all changes", state["project_id"])
    return _with_cost(state, {"phase": "testing"}, cost)


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
        return {"phase": "blocked", "curator_source": "cost_wait", "pending_approval": None}
    return {
        "phase": "testing",
        "curator_source": "cost_wait",
        "pending_approval": None,
    }


def route_after_cost_wait(state: ProjectState) -> str:
    """Always routes through memory_curator -- see route_after_plan_wait's
    docstring for why blocked isn't short-circuited here."""
    return "memory_curator"


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
    Coder can fix specific issues. Either outcome sets ``curator_source`` --
    the Tester makes no LLM calls itself, but both outcomes are curation-worthy
    events (a fix pattern to remember on success, a failure pattern on retry).
    """
    logger.info("project %s: running tests", state["project_id"])

    gate_mode = state.get("coverage_gate_mode", "hard")

    outcome = asyncio.run(
        run_tests(
            executor_client,
            state["project_id"],
            threshold=state["coverage_threshold"],
            gate_mode=gate_mode,
            stack=state["stack"],
        )
    )

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
            "curator_source": "testing",
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
        "curator_source": "testing",
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
        return {"phase": "blocked", "curator_source": "merge_wait", "pending_approval": None}
    return {
        "phase": "done",
        "curator_source": "merge_wait",
        "pending_approval": None,
    }


def route_after_merge_wait(state: ProjectState) -> str:
    """Always routes through memory_curator -- see route_after_plan_wait's
    docstring for why blocked isn't short-circuited here."""
    return "memory_curator"


def node_done(
    state: ProjectState,
    executor_client: ExecutorClient,
) -> dict[str, Any]:
    """Terminal node; commits and (when configured) pushes/opens a PR.

    Only reachable after an approved merge_wait (route_after_memory_curator
    only ever routes here with phase=="done"), so a local commit always
    happens autonomously here (TODO.md Phase 13's goal) -- ``git_branch``
    defaults to ``feature/{project_id}`` when the state field isn't set.
    ``git_remote_url`` has no such default: nothing in the graph populates it
    yet (that needs the project registry's ``git_remote`` threaded into
    ProjectState, which is the same pre-existing gap Phase 7/8 already flagged
    and deferred -- see their TODO.md verification notes), so a project with
    no configured remote simply gets an autonomous local commit and no
    push/PR, rather than this node silently doing nothing at all.

    ``pr_mode`` only matters once a remote is actually configured:

    - **auto**: opens a PR via the GitHub API
    - **manual**: reports the pushed branch name for manual PR creation
    - **configurable / other**: pushes the branch only (no PR automation)
    """
    project_id = state["project_id"]
    branch = state.get("git_branch") or f"feature/{project_id}"
    remote_url = state.get("git_remote_url")
    pr_mode = state.get("pr_mode", "manual")
    integrator = IntegratorClient(executor_client)

    pushed_branch, pushed_remote = asyncio.run(
        integrator.commit_and_push(
            project_id,
            branch_name=branch,
            remote_url=remote_url,
            message=f"chore: merge {project_id}",
        )
    )

    if not pushed_remote:
        logger.info("project %s: committed to %s (no remote)", project_id, pushed_branch)
        return {"phase": "done"}

    if pr_mode == "auto":
        success, detail = asyncio.run(
            integrator.create_pull_request(
                title=f"Merge {project_id}",
                body=f"Auto-generated PR for project **{project_id}**.\n\nAll tests passed. Ready for review.",
                head_branch=pushed_branch,
                base_branch="main",
                remote_url=remote_url,
            )
        )
        if success:
            logger.info("project %s: PR opened — %s", project_id, detail)
        else:
            logger.warning("project %s: PR creation failed — %s", project_id, detail)
    else:
        logger.info("project %s: pushed to %s/%s (pr_mode=%s)", project_id, remote_url, pushed_branch, pr_mode)

    return {"phase": "done"}


def node_memory_curator(
    state: ProjectState,
    projects_root: Path,
    conn: sqlite3.Connection,
    router: ModelRouter,
) -> dict[str, Any]:
    """Runs the Memory Curator after phase transitions and approval resolutions.

    Distills the project's raw events into a markdown summary for LEARNINGS.md
    and zero or more global memory rows (cross-project lessons). This lives
    as its own node -- rather than being called inline from each producer
    node (node_plan_wait, node_cost_wait, node_merge_wait, node_testing,
    node_coder's blocked path) -- so every one of those nodes can stay a
    plain interrupt()-safe or LLM-only node, and so curation always runs
    *after* a transition has already been committed to state, not before.
    Skipped when ``curator_source`` is None -- used as a no-op pass-through
    so the graph can route through this node without re-curating on every
    execution.

    Args:
        state: The current project state, including ``curator_source`` that
            indicates which transition triggered curation.
        projects_root: The projects/ root directory for per-project memory files.
        conn: An open global memory connection from build_global_memory_connection().
        router: A ModelRouter to resolve the "memory_curator" role from.

    Returns:
        A dict with the accumulated cost_usd_session/cost_usd_weekly delta
        when curation actually ran and succeeded; otherwise an empty dict
        (the curator's other side effects are on disk and in sqlite, not in
        the returned state update).
    """
    source = state.get("curator_source")
    if not source:
        return {}

    project_id = state["project_id"]
    phase_events = [msg.content for msg in state["messages"][-20:]]
    if not phase_events:
        logger.info("project %s: nothing to curate (no events)", project_id)
        return {}

    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    try:
        _, ai_message = asyncio.run(
            curate_and_store_phase(
                router,
                projects_root,
                conn,
                project_id,
                phase_events,
                created_at=created_at,
            )
        )
        cost = router.cost_usd(CURATOR_ROLE, ai_message.usage_metadata)
        logger.info("project %s: memory curated from %d event(s)", project_id, len(phase_events))
        return _with_cost(state, {}, cost)
    except Exception:
        logger.warning("project %s: curator failed — continuing without curation", project_id)
        return {}


def route_after_memory_curator(state: ProjectState) -> str:
    """Routes to the correct next node based on what triggered curation.

    Handles all paths through memory_curator:

    - **blocked** (any source, including the Coder's exhausted-retries path):
      halts with END.
    - **testing** (source): tests passed → ``merge_wait``; tests failed
      (phase=implementing) → back to ``coder``.
    - **plan_wait / cost_wait / merge_wait** (approval resolved): routes to the
      phase set by the wait node (task_decomposition, testing, or done).

    Args:
        state: The current project state with ``curator_source`` and ``phase``.

    Returns:
        The next node name or END to halt the run.
    """
    source = state.get("curator_source")

    if state["phase"] == "blocked":
        return END

    if source == "testing":
        if state["phase"] == "awaiting_merge_approval":
            return "merge_wait"
        return "coder"

    if state["phase"] == "implementing":
        return "task_decomposition"
    if state["phase"] == "testing":
        return "testing"
    if state["phase"] == "done":
        return "done"

    logger.warning(
        "project %s: curator has no route for phase=%s source=%s",
        state.get("project_id"), state.get("phase"), source,
    )
    return END
