"""Nodes for the per-project subgraph (ARCHITECTURE.md §4.7, §6.1-6.2).

The Planner (Phase 8), Coder (Phase 9), Reviewer (Phase 10), and Tester
(Phase 11) agents all replace what used to be single stub "planning"/
"implementing"/"testing" nodes with real flows: interview -> draft -> critique
-> approval, task-decomposition -> implement -> retry, review -> approve/revise,
and pytest + coverage gate. A deterministic `static_checks` gate (Phase 23,
ADR-046) sits between Coder and Reviewer -- no LLM calls, so it never touches
cost tracking -- running the stack's linters/type-checkers so a Reviewer token
is never spent on output that a cheap deterministic check would have caught;
the full chain is Coder -> static_checks -> Reviewer -> Tester. The Git/GitHub
Integrator (Phase 13) turns the terminal "done" node into a real commit/push/PR
step, and the Memory Curator (Phase 14) runs after every phase-transition edge
and approval resolution via its own `memory_curator` node rather than being
called inline from each of those nodes -- see `node_memory_curator`'s
docstring for why.

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
revision of the current one. Both retry budgets are capped -- `MAX_TASK_ATTEMPTS`
per task, `MAX_REPLANS` per project -- and when a task exhausts its attempts or
the Reviewer decides the plan itself is wrong, the trigger proposes a replan
rather than immediately halting: `node_replan_wait` (ADR-047) is that
budget's own producer/wait gate, structurally identical to `node_plan_wait`
et al., pausing for a human decision before the Planner is ever re-entered.

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
from services.orchestrator.agents.reviewer import REVIEWER_ROLE, ReviewFinding, review_changes
from services.orchestrator.agents.static_checks import run_static_checks
from services.orchestrator.agents.tester import run_tests
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.cost_aggregator import other_projects_cost_usd_weekly
from services.orchestrator.graph.state import ProjectState
from services.orchestrator.integrator_client import IntegratorClient
from services.orchestrator.memory.global_store import GlobalMemoryConnection, recall_for_task
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.progress_buffer import add_progress
from services.orchestrator.stacks import StacksConfig

logger = logging.getLogger(__name__)

MAX_TASK_ATTEMPTS = 3
MAX_REPLANS = 2


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


def _task_progress_lines(task_queue: list[dict], recent_done_limit: int = 10) -> list[str]:
    """Renders each task_queue item as a one-line status summary for the
    Memory Curator's input, e.g. "[done] Add a User model (attempts: 1)".
    Every non-done task is kept (always actionable/interesting); done tasks
    are capped to the most recent `recent_done_limit` so a long-lived
    project's curator prompt doesn't grow with total completed-task count
    forever."""
    done_indices = [i for i, task in enumerate(task_queue) if task["status"] == "done"]
    keep_done = set(done_indices[-recent_done_limit:])
    kept = [task for i, task in enumerate(task_queue) if task["status"] != "done" or i in keep_done]
    return [
        f"[{task['status']}] {task['description']} (attempts: {task['attempts']})" for task in kept
    ]


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
    cursor = max(1, state["messages_curated_through"])
    tail = [message.content for message in messages[cursor:]]
    summary = state["messages_summary"]
    qa_history = ([summary] if summary else []) + tail
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
    """Breaks the approved plan into task_queue, once, right after plan approval.

    On a replan (ADR-047), previously completed tasks are carried forward as
    ``done`` and every blocked/pending entry from the failed plan is dropped
    -- the redrafted plan's decomposition starts fresh only for the
    remaining work, so completed work is never re-planned or re-implemented.
    On first decomposition ``task_queue`` is empty, so this is a no-op.
    """
    done_tasks = [task for task in state["task_queue"] if task["status"] == "done"]
    tasks, ai_message = asyncio.run(
        decompose_plan_into_tasks(
            router,
            state["plan"],
            completed_task_descriptions=[task["description"] for task in done_tasks],
        )
    )
    cost = router.cost_usd(CODER_ROLE, ai_message.usage_metadata)
    new_tasks = [
        {"description": task.description, "status": "pending", "attempts": 0} for task in tasks
    ]
    task_queue = done_tasks + new_tasks
    logger.info("project %s: decomposed into %d task(s)", state["project_id"], len(task_queue))
    return _with_cost(
        state,
        {"task_queue": task_queue, "phase": "implementing"},
        cost,
    )


def _replace_task(task_queue: list[dict], index: int, task: dict) -> list[dict]:
    """Returns a new task_queue list with the item at `index` replaced by `task`."""
    return [task if i == index else existing for i, existing in enumerate(task_queue)]


def _replan_payload(state: ProjectState, reason: str, source: str) -> dict[str, Any]:
    """Builds the replan_wait interrupt payload (ADR-047).

    Args:
        state: The current project state.
        reason: Why a replan is being proposed (reviewer reason or the
            blocked task's description and attempt history).
        source: Which trigger produced this -- "reviewer" or "coder".

    Returns:
        A pending_approval dict for the Gateway's generic renderer.
    """
    return {
        "kind": "replan_approval",
        "thread_id": state["project_id"],
        "project_id": state["project_id"],
        "trigger": source,
        "reason": reason,
        "replan_count": state["replan_count"],
        "summary": (
            f"Plan appears broken ({source}): {reason} -- approve a replan? "
            f"({state['replan_count']}/{MAX_REPLANS} replans used)"
        ),
    }


def node_coder(
    state: ProjectState,
    router: ModelRouter,
    executor_client: ExecutorClient,
    memory_conn: GlobalMemoryConnection | None = None,
) -> dict[str, Any]:
    """Implements one task-or-retry-attempt per execution.

    No `interrupt()` call happens anywhere in this node (the Coder is fully
    autonomous within its retry budget), so -- unlike the Planner/cost/merge
    gates -- there's no LangGraph replay concern here: this node can safely
    make LLM and Executor calls directly. Every task_queue item carries its
    own `status` and `attempts` count (ADR-034); the active task is whichever
    item has status "in_progress" (promoted from the first "pending" item if
    none is active yet), rather than a separate `current_task` field.

    When `memory_conn` is given (every real deployment -- graph/build.py
    wires the global memory connection through), cross-project lessons
    matching the active task's description are recalled and fed into the
    Coder's prompt, closing Phase 14's loop: a fix recorded while working on
    project A actually reaches project B's Coder, instead of the global
    store being write-only in production. None keeps recall off for tests
    that only exercise task/queue mechanics.

    At exhaustion -- the active task's `attempts` reaching `MAX_TASK_ATTEMPTS`
    -- the task's status becomes "blocked"; if the project is still under the
    replan cap this also proposes a replan approval (ADR-047) instead of
    retrying the same task again, otherwise the project phase itself goes to
    "blocked".
    """
    task_queue = state["task_queue"]
    active_index = next(
        (i for i, task in enumerate(task_queue) if task["status"] == "in_progress"), None
    )
    if active_index is None:
        active_index = next(
            (i for i, task in enumerate(task_queue) if task["status"] == "pending"), None
        )
        if active_index is None:
            return {"phase": "reviewing"}

    active_task = {**task_queue[active_index], "status": "in_progress"}
    recalled_lessons = (
        recall_for_task(memory_conn, active_task["description"]) if memory_conn is not None else []
    )
    result = asyncio.run(
        run_coder_task(
            router,
            executor_client,
            state["project_id"],
            active_task["description"],
            stack=state["stack"],
            recalled_lessons=recalled_lessons,
        )
    )
    new_changed_files = list(dict.fromkeys(state["changed_files"] + result.written_files))

    if result.success:
        active_task["status"] = "done"
        new_queue = _replace_task(task_queue, active_index, active_task)
        remaining = sum(1 for task in new_queue if task["status"] in ("pending", "in_progress"))
        logger.info("project %s: task succeeded, %d remaining", state["project_id"], remaining)
        add_progress(state["project_id"], f"✓ task succeeded ({remaining} remaining)")
        return _with_cost(
            state,
            {
                "task_queue": new_queue,
                "phase": "implementing" if remaining else "reviewing",
                "changed_files": new_changed_files,
            },
            result.cost_usd,
        )

    active_task["attempts"] += 1
    if active_task["attempts"] >= MAX_TASK_ATTEMPTS:
        active_task["status"] = "blocked"
        new_queue = _replace_task(task_queue, active_index, active_task)
        if state["replan_count"] < MAX_REPLANS:
            logger.warning(
                "project %s: task exhausted %d attempts, proposing replan",
                state["project_id"],
                active_task["attempts"],
            )
            add_progress(
                state["project_id"],
                f"↻ task exhausted {active_task['attempts']} attempts, proposing replan",
            )
            reason = (
                f"task {active_task['description']!r} failed {active_task['attempts']} attempts"
            )
            return _with_cost(
                state,
                {
                    "task_queue": new_queue,
                    "phase": "awaiting_replan_approval",
                    "curator_source": "coder",
                    "changed_files": new_changed_files,
                    "pending_approval": _replan_payload(state, reason, "coder"),
                },
                result.cost_usd,
            )
        logger.warning(
            "project %s: task exhausted %d attempts at replan cap, blocking",
            state["project_id"],
            active_task["attempts"],
        )
        add_progress(
            state["project_id"], f"✗ task blocked after {active_task['attempts']} attempts"
        )
        return _with_cost(
            state,
            {
                "task_queue": new_queue,
                "phase": "blocked",
                "curator_source": "coder",
                "changed_files": new_changed_files,
            },
            result.cost_usd,
        )

    logger.info(
        "project %s: task attempt %d failed, retrying",
        state["project_id"],
        active_task["attempts"],
    )
    add_progress(state["project_id"], f"↻ task attempt {active_task['attempts']} failed, retrying")
    new_queue = _replace_task(task_queue, active_index, active_task)
    return _with_cost(
        state,
        {"task_queue": new_queue, "phase": "implementing", "changed_files": new_changed_files},
        result.cost_usd,
    )


def route_after_coder(state: ProjectState) -> str:
    """Loops back for the next task/retry, curates then halts if blocked, routes
    to the deterministic checks gate when done, or to the Memory Curator on
    the way to the replan gate when a task exhaustion proposed one (ADR-047)."""
    if state["phase"] == "blocked":
        return "memory_curator"
    if state["phase"] == "awaiting_replan_approval":
        return "memory_curator"
    if state["phase"] == "reviewing":
        return "static_checks"
    return "coder"


def _reopen_last_done_task_or_block(
    state: ProjectState, feedback: AIMessage, curator_source: str
) -> dict[str, Any]:
    """Reopens the most recently completed task for another attempt.

    Shared by node_static_checks and node_plugin (Task 7; a 'gating'
    plugin's changes-requested verdict, graph/plugin_node.py) -- both feed
    a gating finding back to the Coder the same way: reopen the last-done task,
    honoring MAX_TASK_ATTEMPTS, or block the project if there's no
    completed task to reopen at all.

    Args:
        state: The current project state.
        feedback: The failure/finding message attached to `messages`.
        curator_source: Recorded as `curator_source` on a blocked outcome,
            so the Memory Curator's summary can say what blocked it.

    Returns:
        A state update: task_queue/phase="implementing" on a normal
        reopen; task_queue/phase="blocked" at MAX_TASK_ATTEMPTS; just
        phase="blocked" (no task_queue key) when there's no done task at all.
    """
    task_queue = state["task_queue"]
    last_done_index = next(
        (i for i in range(len(task_queue) - 1, -1, -1) if task_queue[i]["status"] == "done"),
        None,
    )
    if last_done_index is None:
        return {"phase": "blocked", "curator_source": curator_source, "messages": [feedback]}

    reopened = {
        **task_queue[last_done_index],
        "status": "in_progress",
        "attempts": task_queue[last_done_index]["attempts"] + 1,
    }
    if reopened["attempts"] >= MAX_TASK_ATTEMPTS:
        reopened["status"] = "blocked"
        return {
            "task_queue": _replace_task(task_queue, last_done_index, reopened),
            "phase": "blocked",
            "curator_source": curator_source,
            "messages": [feedback],
        }
    return {
        "task_queue": _replace_task(task_queue, last_done_index, reopened),
        "phase": "implementing",
        "messages": [feedback],
    }


def node_static_checks(
    state: ProjectState,
    executor_client: ExecutorClient,
    stacks_config: StacksConfig,
) -> dict[str, Any]:
    """Runs the stack's deterministic checks before any Reviewer token is spent.

    Sits between coder and reviewer (ADR-046). Makes no LLM calls and never
    touches cost tracking. On pass (including nothing applicable to run),
    control continues to the Reviewer exactly as before this node existed.
    On failure, the most recently completed task is reopened -- status back
    to "in_progress" with attempts incremented, honoring MAX_TASK_ATTEMPTS
    and the existing blocked semantics -- and the raw check output is
    attached to messages as the Coder's feedback. A failure with no
    reopenable task (empty or never-completed queue) blocks the project
    rather than letting broken code through to review.

    Args:
        state: The current project state (phase "reviewing" on entry).
        executor_client: The Executor client checks run through.
        stacks_config: The loaded stack registry (ADR-045).

    Returns:
        A state update: {} on pass; task_queue/phase/messages updates on
        failure; a blocked transition when attempts are exhausted.
    """
    outcome = asyncio.run(
        run_static_checks(executor_client, state["project_id"], state["stack"], stacks_config)
    )
    if outcome.passed:
        logger.info(
            "project %s: static checks passed (%d ran, %d skipped)",
            state["project_id"],
            len(outcome.ran),
            len(outcome.skipped),
        )
        return {}

    failure_lines = ["Static checks failed:"]
    for failure in outcome.failures:
        failure_lines.append(
            f"$ {' '.join(failure.command)} (exit {failure.exit_code})\n{failure.output}"
        )
    feedback = AIMessage(content="\n\n".join(failure_lines))
    update = _reopen_last_done_task_or_block(state, feedback, "static_checks")

    if update["phase"] != "blocked":
        logger.info(
            "project %s: static checks failed (%d finding set(s)), reopening task",
            state["project_id"],
            len(outcome.failures),
        )
    elif "task_queue" not in update:
        logger.warning(
            "project %s: static checks failed with no completed task to reopen",
            state["project_id"],
        )
    else:
        blocked_task = next(t for t in update["task_queue"] if t["status"] == "blocked")
        logger.warning(
            "project %s: static checks exhausted %d attempts, blocking",
            state["project_id"],
            blocked_task["attempts"],
        )
    return update


def route_after_static_checks(state: ProjectState) -> str:
    """Routes to Coder on failure, curator on blocked, Reviewer on pass."""
    if state["phase"] == "blocked":
        return "memory_curator"
    if state["phase"] == "implementing":
        return "coder"
    return "reviewer"


def route_after_testing(state: ProjectState) -> str:
    """Routes a test failure straight to the Memory Curator, skipping any
    after_tester plugin chain entirely -- a failing run isn't ready for an
    additional review pass, and node_testing's failure path must always
    reach curation the same way it did before the after_tester slot existed
    (ADR-046's precedent: static_checks/reviewer are filtered the same way).
    Only a passing run continues into the slot's plugin chain (or straight to
    memory_curator if the slot is empty, since the entry node IS
    memory_curator in that case)."""
    if state["phase"] == "implementing":
        return "memory_curator"
    return "after_tester"


def node_reviewer(
    state: ProjectState, router: ModelRouter, executor_client: ExecutorClient
) -> dict[str, Any]:
    """Reviews project files after the Coder finishes implementing.

    Reads only changed files (from state["changed_files"]) when populated,
    otherwise falls back to reading every file in the workspace container
    via the Executor client. Sends files to an LLM with a review prompt
    and gets back a schema-forced verdict (approve / changes-requested).
    Changes-requested routes back to the Coder with findings attached;
    approve advances to frontend_review.

    When the result also carries `replan_needed` (ADR-047), the plan itself
    -- not just the diff -- is treated as broken: under the replan cap this
    arms the replan gate (`awaiting_replan_approval`) instead of routing back
    to Coder for another edit the Reviewer already said won't help; at the
    cap it degrades back to the ordinary changes-requested path, with a
    critical finding appended documenting that the cap was hit. That
    degraded verdict routes to Coder as usual, but termination isn't
    guaranteed: if every task_queue item is already "done", node_coder finds
    nothing pending/in_progress and no-ops back to "reviewing" without acting
    on the finding, so the cycle only ends once the Reviewer approves -- a
    pre-existing Phase-10-era gap this cap doesn't close (tracked as a Phase
    25 follow-up).
    """
    result, ai_message = asyncio.run(
        review_changes(
            router, executor_client, state["project_id"], changed_files=state["changed_files"]
        )
    )
    cost = router.cost_usd(REVIEWER_ROLE, ai_message.usage_metadata) if ai_message else 0.0

    if result.replan_needed and state["replan_count"] < MAX_REPLANS:
        logger.warning(
            "project %s: reviewer proposes replan: %s",
            state["project_id"],
            result.replan_reason,
        )
        return _with_cost(
            state,
            {
                "phase": "awaiting_replan_approval",
                "curator_source": "reviewer",
                "pending_approval": _replan_payload(state, result.replan_reason, "reviewer"),
            },
            cost,
        )
    if result.replan_needed:
        logger.warning(
            "project %s: reviewer proposed replan at cap, degrading to revision",
            state["project_id"],
        )
        result = result.model_copy(
            update={
                "verdict": "changes-requested",
                "findings": list(result.findings)
                + [
                    ReviewFinding(
                        file="(plan)",
                        line=0,
                        severity="critical",
                        message=f"replan cap reached: {result.replan_reason}",
                    )
                ],
            }
        )

    if result.verdict == "changes-requested":
        logger.info(
            "project %s: reviewer requested changes (%d finding(s))",
            state["project_id"],
            len(result.findings),
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
    """Routes back to Coder for revisions, onward to frontend review, or --
    when the Reviewer proposed a replan under the cap (ADR-047) -- to the
    Memory Curator on the way to the replan gate."""
    if state["phase"] == "awaiting_replan_approval":
        return "memory_curator"
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
            state["project_id"],
            len(findings),
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


def node_cost_check(state: ProjectState, checkpointer: Any, threshold_usd: float) -> dict[str, Any]:
    """Trips the cost-threshold gate if the *global* weekly total is at or above threshold_usd.

    The gate is global, not per-project (TODO.md Phase 18, ADR-009): sums
    every other known project's latest checkpointed cost_usd_weekly
    (other_projects_cost_usd_weekly) plus this project's own live
    cost_usd_weekly from state. Returns an empty update (no gate) unless
    that combined total has reached threshold_usd, in which case it commits
    the pending approval payload node_cost_wait will interrupt on next.
    """
    global_total = state["cost_usd_weekly"] + other_projects_cost_usd_weekly(
        checkpointer, exclude_project_id=state["project_id"]
    )
    if global_total < threshold_usd:
        return {}
    logger.info(
        "project %s: global cost threshold reached (%.2f >= %.2f)",
        state["project_id"],
        global_total,
        threshold_usd,
    )
    return {
        "phase": "awaiting_cost_approval",
        "pending_approval": {
            "kind": "cost_alert",
            "thread_id": state["project_id"],
            "project_id": state["project_id"],
            "cost_usd_weekly_global": global_total,
            "threshold_usd": threshold_usd,
            "summary": (
                f"Crossed ${global_total:.2f} of "
                f"${threshold_usd:.2f} global weekly cost limit -- continue?"
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
        elif outcome.coverage_pct is not None:
            feedback_lines = [
                f"Coverage {outcome.coverage_pct:.1f}% below threshold "
                f"{state['coverage_threshold']}% (gate mode: {gate_mode})"
            ]
            uncovered = _parse_uncovered_lines(outcome.report)
            if uncovered:
                feedback_lines.append("Uncovered lines:")
                feedback_lines.extend(f"- {entry}" for entry in uncovered)
        else:
            feedback_lines = [
                "Test run failed with no parseable FAILED lines -- likely a "
                "collection or import error. Report tail:",
                outcome.report[-2000:],
            ]

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


def node_replan_wait(state: ProjectState) -> dict[str, Any]:
    """Pauses for a human decision on redrafting the plan (ADR-047).

    Approve sends the graph back to the Planner's draft step with the
    failure context injected into messages -- the redrafted plan then goes
    through the normal critique and plan-approval gates again, so a human
    still signs off on the actual new plan. Anything else halts exactly as
    a blocked project does today. replan_count is incremented here, the
    single place a replan is actually granted.
    """
    decision = interrupt(state["pending_approval"])
    if decision != "approve":
        return {
            "phase": "blocked",
            "curator_source": "replan_wait",
            "pending_approval": None,
        }
    payload = state["pending_approval"] or {}
    context = (
        "Replan approved. The previous plan failed: "
        f"{payload.get('reason', 'unknown reason')} (trigger: "
        f"{payload.get('trigger', 'unknown')}). Draft a corrected plan; "
        "completed tasks will be preserved and must not be re-planned."
    )
    return {
        "phase": "planning",
        "curator_source": "replan_wait",
        "replan_count": state["replan_count"] + 1,
        "messages": [HumanMessage(content=context)],
        "pending_approval": None,
    }


def route_after_replan_wait(state: ProjectState) -> str:
    """Always routes through memory_curator -- see route_after_plan_wait's
    docstring for why blocked isn't short-circuited here."""
    return "memory_curator"


def node_done(
    state: ProjectState,
    executor_client: ExecutorClient,
    secrets_dir: Path | None = None,
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

    Args:
        state: The project's current graph state.
        executor_client: The Executor client to run git commands through.
        secrets_dir: Directory containing Docker secret files, forwarded to
            IntegratorClient. Defaults to /run/secrets; overridable so tests
            can point at a tmp_path instead.
    """
    project_id = state["project_id"]
    branch = state.get("git_branch") or f"feature/{project_id}"
    remote_url = state.get("git_remote_url")
    pr_mode = state.get("pr_mode", "manual")
    integrator = IntegratorClient(executor_client, secrets_dir=secrets_dir)

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
                body=(
                    f"Auto-generated PR for project **{project_id}**.\n\n"
                    "All tests passed. Ready for review."
                ),
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
        logger.info(
            "project %s: pushed to %s/%s (pr_mode=%s)",
            project_id,
            remote_url,
            pushed_branch,
            pr_mode,
        )

    return {"phase": "done"}


def node_memory_curator(
    state: ProjectState,
    projects_root: Path,
    conn: GlobalMemoryConnection,
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
        conn: A GlobalMemoryConnection from build_global_memory_connection()
            (sqlite3.Connection + a lock serializing concurrent access to it).
        router: A ModelRouter to resolve the "memory_curator" role from.

    Returns:
        A dict with the accumulated cost_usd_session/cost_usd_weekly delta,
        plus messages_summary and messages_curated_through, when curation
        actually ran and succeeded; otherwise an empty dict (the curator's
        other side effects are on disk and in sqlite, not in the returned
        state update).
    """
    source = state.get("curator_source")
    if not source:
        return {}

    project_id = state["project_id"]
    cursor = state["messages_curated_through"]
    phase_events = [msg.content for msg in state["messages"][cursor:]]
    if state["messages"]:
        phase_events = phase_events + _task_progress_lines(state["task_queue"])
    if not phase_events:
        logger.info("project %s: nothing to curate (no events)", project_id)
        return {}

    created_at = datetime.datetime.now(datetime.UTC).isoformat()

    try:
        result, ai_message = asyncio.run(
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
        new_summary = (
            f"{state['messages_summary']}\n\n{result.summary_markdown}"
            if state["messages_summary"]
            else result.summary_markdown
        )
        return _with_cost(
            state,
            {
                "messages_summary": new_summary,
                "messages_curated_through": len(state["messages"]),
            },
            cost,
        )
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
    - **coder / reviewer** (source, phase=awaiting_replan_approval, ADR-047):
      routes to ``replan_wait`` for a human decision on the proposed replan.
    - **replan_wait** (source, approval resolved): phase=planning (approved)
      routes to ``planner_draft`` to redraft; blocked (rejected) is already
      handled by the phase=="blocked" check above.

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

    if state["phase"] == "awaiting_replan_approval":
        return "replan_wait"
    if source == "replan_wait" and state["phase"] == "planning":
        return "planner_draft"

    if state["phase"] == "implementing":
        return "task_decomposition"
    if state["phase"] == "testing":
        return "testing"
    if state["phase"] == "done":
        return "done"

    logger.warning(
        "project %s: curator has no route for phase=%s source=%s",
        state.get("project_id"),
        state.get("phase"),
        source,
    )
    return END
