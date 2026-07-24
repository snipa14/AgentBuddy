"""End-to-end tests for the compiled project subgraph.

Uses a real SqliteSaver against a tmp_path file rather than an in-memory
saver specifically so `test_pending_interrupt_survives_a_simulated_restart`
can prove persistence: an ephemeral in-memory checkpointer would trivially
"survive" a restart within the same test process, proving nothing.

Every run now passes through the Planner's interview -> draft -> critique
-> plan-approval gate (Phase 8), the Coder's task decomposition/tool-calling
loop (Phase 9), the Reviewer (Phase 10), and the Memory Curator sitting on
every phase-transition edge (Phase 14) before it can ever reach the cost/
merge gates -- see planner_fakes.py's fast_track_router() for the scripted
LLM responses that sail a test straight through when a test cares about what
happens *after* them, not the interview or coding itself. The Memory
Curator's own LLM call is a no-op whenever `messages` is still empty
(node_memory_curator's early-return guard), which every fast-track path
here satisfies, so fast_track_router() never needs extra scripted responses
for curation. _UNUSED_EXECUTOR_CLIENT is a stub that reports a clean passing
test run and successful git commands for any test that reaches node_testing/
node_done without caring about the specifics.
"""

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

from services.orchestrator.agents.coder import CoderTask, _TaskList
from services.orchestrator.agents.planner import InterviewTurn
from services.orchestrator.executor_client import CommandResult, TestResult
from services.orchestrator.graph.build import DEFAULT_GLOBAL_COST_THRESHOLD_USD, build_project_graph
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.nodes import MAX_REPLANS, MAX_TASK_ATTEMPTS
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import (
    CODER_DONE,
    CURATION_OK,
    EMPTY_DRAFT,
    NO_FINDINGS_CRITIQUE,
    ONE_TASK,
    READY_TURN,
    REVIEWER_APPROVE,
    FakeRouter,
    fast_track_router,
    structured,
)
from services.orchestrator.stacks import StacksConfig


class _StubExecutorClient:
    """Placeholder ExecutorClient for tests that drive the graph past
    node_testing/node_done but don't care about real test execution or git
    output -- always reports a clean passing test run and a successful git
    command."""

    async def run_tests(
        self, project_id: str, framework: str = "pytest", stack: str = "python"
    ) -> TestResult:
        return TestResult(passed=1, failed=0, coverage_pct=100.0, report="1 passed")

    async def run_command(
        self, project_id: str, command, timeout=None, stack: str = "python"
    ) -> CommandResult:
        return CommandResult(stdout="", stderr="", exit_code=0)


_UNUSED_EXECUTOR_CLIENT = _StubExecutorClient()


def _config(project_id: str) -> dict:
    return {"configurable": {"thread_id": project_id}}


def test_planner_interview_asks_a_question_before_reaching_plan_approval(
    tmp_path, memory_conn, projects_root
):
    router = FakeRouter(
        [
            structured(InterviewTurn(status="questions", questions=["What language?"])),
            READY_TURN,
            EMPTY_DRAFT,
            NO_FINDINGS_CRITIQUE,
        ]
    )
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        router,
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-interview")

    graph.invoke(
        new_project_state("proj-interview", initial_message="build a thing"), config=config
    )

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "planning"
    assert snapshot.values["pending_approval"] == {
        "kind": "plan_questions",
        "thread_id": "proj-interview",
        "project_id": "proj-interview",
        "questions": ["What language?"],
        "summary": "- What language?",
    }
    assert snapshot.next == ("planner_wait",)
    assert any(task.interrupts for task in snapshot.tasks)

    graph.invoke(Command(resume="Python"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_plan_approval"
    assert snapshot.values["pending_approval"]["kind"] == "plan_approval"
    assert snapshot.next == ("plan_wait",)


def test_project_below_cost_threshold_reaches_plan_approval_then_merge_gate(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-under-budget")

    graph.invoke(new_project_state("proj-under-budget"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_plan_approval"
    assert snapshot.next == ("plan_wait",)

    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_merge_approval"
    assert snapshot.values["pending_approval"] == {
        "kind": "merge_approval",
        "thread_id": "proj-under-budget",
        "project_id": "proj-under-budget",
        "summary": "Project proj-under-budget: tests passed -- merge to main?",
    }
    assert snapshot.next == ("merge_wait",)
    assert any(task.interrupts for task in snapshot.tasks)


def test_cost_interrupt_fires_at_threshold(tmp_path, memory_conn, projects_root):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-over-budget")
    graph.invoke(
        new_project_state("proj-over-budget", cost_usd_weekly=DEFAULT_GLOBAL_COST_THRESHOLD_USD),
        config=config,
    )
    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_cost_approval"
    assert snapshot.values["pending_approval"]["kind"] == "cost_alert"
    assert snapshot.next == ("cost_wait",)
    assert any(task.interrupts for task in snapshot.tasks)


def test_resume_approve_after_cost_interrupt_reaches_merge_gate(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-resume-continue")
    graph.invoke(
        new_project_state("proj-resume-continue", cost_usd_weekly=DEFAULT_GLOBAL_COST_THRESHOLD_USD),
        config=config,
    )
    graph.invoke(Command(resume="approve"), config=config)

    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_merge_approval"
    assert snapshot.next == ("merge_wait",)


def test_resume_reject_after_cost_interrupt_blocks_and_halts_the_run(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-resume-pause")
    graph.invoke(
        new_project_state("proj-resume-pause", cost_usd_weekly=DEFAULT_GLOBAL_COST_THRESHOLD_USD),
        config=config,
    )
    graph.invoke(Command(resume="approve"), config=config)

    graph.invoke(Command(resume="reject"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "blocked"
    assert snapshot.next == ()


def test_resume_reject_at_merge_gate_blocks_and_halts_the_run(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-reject-merge")
    graph.invoke(new_project_state("proj-reject-merge"), config=config)
    graph.invoke(Command(resume="approve"), config=config)

    graph.invoke(Command(resume="reject"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "blocked"
    assert snapshot.next == ()


def test_resume_approve_at_merge_gate_reaches_done(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-approve-merge")
    graph.invoke(new_project_state("proj-approve-merge"), config=config)
    graph.invoke(Command(resume="approve"), config=config)

    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "done"
    assert snapshot.next == ()


def test_pending_interrupt_survives_a_simulated_restart(
    tmp_path, memory_conn, projects_root
):
    """Proves checkpointer persistence, not just in-memory graph state.

    Builds the graph, gets through plan approval and trips the cost gate,
    then closes the sqlite connection and opens a brand-new one against the
    same file (simulating `./foundry down` followed by `./foundry up`)
    before resuming -- exactly the scenario Phase 5's Definition of Done
    requires. The Planner isn't called again after the restart, so the
    post-restart router never needs any scripted responses.
    """
    db_path = tmp_path / "foundry_memory.db"
    project_id = "proj-restart"
    config = _config(project_id)

    checkpointer_before_restart = build_sqlite_checkpointer(db_path)
    graph_before_restart = build_project_graph(
        checkpointer_before_restart,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    graph_before_restart.invoke(
        new_project_state(project_id, cost_usd_weekly=DEFAULT_GLOBAL_COST_THRESHOLD_USD),
        config=config,
    )
    graph_before_restart.invoke(Command(resume="approve"), config=config)
    snapshot_before_restart = graph_before_restart.get_state(config)
    assert snapshot_before_restart.values["phase"] == "awaiting_cost_approval"
    checkpointer_before_restart.conn.close()

    checkpointer_after_restart = build_sqlite_checkpointer(db_path)
    graph_after_restart = build_project_graph(
        checkpointer_after_restart,
        FakeRouter([]),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )

    snapshot_after_restart = graph_after_restart.get_state(config)
    assert snapshot_after_restart.values["phase"] == "awaiting_cost_approval"
    assert snapshot_after_restart.values["pending_approval"]["kind"] == "cost_alert"
    assert snapshot_after_restart.next == ("cost_wait",)
    assert any(task.interrupts for task in snapshot_after_restart.tasks)

    graph_after_restart.invoke(Command(resume="approve"), config=config)
    graph_after_restart.invoke(Command(resume="approve"), config=config)

    final_snapshot = graph_after_restart.get_state(config)
    assert final_snapshot.values["phase"] == "done"
    assert final_snapshot.next == ()


def test_cost_interrupt_fires_from_a_lower_configured_global_threshold(
    tmp_path, memory_conn, projects_root
):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        fast_track_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
        global_cost_threshold_usd=1.0,
    )
    config = _config("proj-tiny-budget")

    graph.invoke(new_project_state("proj-tiny-budget", cost_usd_weekly=2.0), config=config)
    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_cost_approval"
    assert snapshot.values["pending_approval"]["threshold_usd"] == 1.0


# -- Definition-of-Done: static_checks gates the Reviewer (ADR-046) ---------------


class _RoleTrackingRouter:
    """Wraps a FakeRouter, recording every `resolve()` call by role and --
    when `forbidden_role` is given -- raising `AssertionError` instead of
    returning a model for that one role, rather than silently letting it
    through.

    Used by the two static_checks DoD tests below to prove the Reviewer LLM
    is (test 1) never reached, or (test 2) reached exactly once, depending
    on whether the deterministic check passes -- resolving the role is the
    earliest observable point a call "would have happened," before any
    response is even popped off the shared FakeChatModel queue.
    """

    def __init__(self, responses: list, forbidden_role: str | None = None):
        self._router = FakeRouter(responses)
        self.resolved_roles: list[str] = []
        self._forbidden_role = forbidden_role

    def resolve(self, role):
        self.resolved_roles.append(role)
        if role == self._forbidden_role:
            raise AssertionError(f"{role} must not be called")
        return self._router.resolve(role)

    def cost_usd(self, role, usage_metadata):
        return self._router.cost_usd(role, usage_metadata)


class _StaticCheckExecutorClient(_StubExecutorClient):
    """Reuses `_StubExecutorClient`'s always-passing `run_tests`/git behavior
    (only relevant to the clean-diff test, which drives far enough to reach
    `node_testing`) but returns a caller-chosen exit code for every
    `run_command` call.

    static_checks (ADR-046) is the only thing that calls `run_command` along
    either of these two tests' driven paths -- the Coder's scripted
    `CODER_DONE` response carries no `tool_calls`, so `run_coder_task` never
    dispatches a tool -- so one canned exit code is all either test needs.
    """

    def __init__(self, check_exit_code: int, check_output: str = ""):
        self.check_exit_code = check_exit_code
        self.check_output = check_output
        self.calls: list[tuple] = []

    async def run_command(
        self, project_id: str, command, timeout=None, stack: str = "python"
    ) -> CommandResult:
        self.calls.append((project_id, tuple(command)))
        return CommandResult(
            stdout=self.check_output, stderr="", exit_code=self.check_exit_code
        )


def test_lint_broken_diff_never_reaches_reviewer(
    tmp_path, memory_conn, projects_root, two_stack_registry
):
    """DoD (ADR-046): a failing check must route back to coder without any
    Reviewer LLM call. The router stub raises AssertionError if the graph
    resolves the 'reviewer' role.

    Scripts exactly the five LLM-shaped responses this driven path can
    legitimately consume: READY_TURN (planner_ask), EMPTY_DRAFT
    (planner_draft), NO_FINDINGS_CRITIQUE (planner_critique), ONE_TASK
    (task_decomposition), CODER_DONE (the Coder's one successful attempt on
    its only task) -- then deliberately leaves the queue empty. static_checks
    itself makes no LLM call; when its failure reopens the just-finished task
    and route_after_static_checks sends control back to "coder" for a second
    attempt, that second attempt's first `.astream()` call pops an empty
    list and raises IndexError -- proof that no sixth LLM call, reviewer or
    otherwise, was ever reached. Per nodes.py's module docstring, a node's
    state update only commits once the node function fully returns, so the
    IndexError raised inside this second (never-returning) node_coder call
    leaves static_checks' committed update -- the reopened task, phase
    "implementing" -- as the last persisted checkpoint.
    """
    router = _RoleTrackingRouter(
        [READY_TURN, EMPTY_DRAFT, NO_FINDINGS_CRITIQUE, ONE_TASK, CODER_DONE],
        forbidden_role="reviewer",
    )
    executor = _StaticCheckExecutorClient(
        check_exit_code=1, check_output="app.py:1:1: F821 undefined name"
    )
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer, router, executor, memory_conn, projects_root, two_stack_registry
    )
    config = _config("proj-lint-broken")

    graph.invoke(new_project_state("proj-lint-broken"), config=config)
    snapshot = graph.get_state(config)
    assert snapshot.next == ("plan_wait",)

    with pytest.raises(IndexError):
        graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "implementing"
    assert snapshot.values["task_queue"][0]["status"] == "in_progress"
    assert snapshot.values["task_queue"][0]["attempts"] == 1
    assert "reviewer" not in router.resolved_roles


def test_clean_diff_reaches_reviewer_exactly_as_before(
    tmp_path, memory_conn, projects_root, two_stack_registry
):
    """A passing check set must leave the pre-static_checks behavior intact:
    the Reviewer's resolve() is called exactly once and the graph proceeds
    past "reviewing".

    Scripts the same six responses fast_track_router() always has -- adding
    static_checks (ADR-046) between coder and reviewer changes nothing about
    LLM call count or order when every applicable check passes, so this
    reuses the exact fast_track_router() shape rather than a bespoke one.
    """
    router = _RoleTrackingRouter(
        [READY_TURN, EMPTY_DRAFT, NO_FINDINGS_CRITIQUE, ONE_TASK, CODER_DONE, REVIEWER_APPROVE]
    )
    executor = _StaticCheckExecutorClient(check_exit_code=0)
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer, router, executor, memory_conn, projects_root, two_stack_registry
    )
    config = _config("proj-lint-clean")

    graph.invoke(new_project_state("proj-lint-clean"), config=config)
    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_merge_approval"
    assert snapshot.next == ("merge_wait",)
    assert router.resolved_roles.count("reviewer") == 1


# -- ADR-047: human-gated replan route ---------------------------------------


_TWO_TASKS = structured(
    _TaskList(tasks=[CoderTask(description="Task A"), CoderTask(description="Task B")])
)


def _looping_tool_call() -> AIMessage:
    """One Coder turn that keeps `run_coder_task`'s native tool-calling loop
    going without ever finishing the task.

    `node_coder` (graph/nodes.py) calls `run_coder_task` without overriding
    its `max_steps=10` default, so a task attempt only comes back
    `CoderTaskResult.success=False` -- the trigger for MAX_TASK_ATTEMPTS
    bookkeeping -- once all 10 turns return tool_calls and the loop exhausts
    max_steps; a single "keep going" response repeated 10 times is a legal
    Coder turn each time (the fake tool dispatches through
    _StubExecutorClient.run_command, which always succeeds), so it never
    raises, it just never stops calling tools before the cap.
    """
    return AIMessage(
        content="",
        tool_calls=[{"name": "run_command", "args": {"command": ["true"]}, "id": "call-loop"}],
        usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
    )


_ONE_FAILED_TASK_ATTEMPT = [_looping_tool_call()] * 10


def _replan_router() -> FakeRouter:
    """Scripts the Planner interview->draft->critique (3 responses), the
    two-task decomposition (_TWO_TASKS), Task A succeeding on its first
    attempt (CODER_DONE), and Task B exhausting MAX_TASK_ATTEMPTS (3) --
    each failing attempt itself exhausting run_coder_task's own max_steps
    (10 looping tool calls, see _looping_tool_call's docstring), for
    3 * 10 = 30 responses.

    35 responses total, consumed across exactly two `.invoke()` calls: the
    first 3 (READY_TURN/EMPTY_DRAFT/NO_FINDINGS_CRITIQUE) by the initial
    `.invoke()` up to `plan_wait`; the remaining 32 by
    `Command(resume="approve")`, which decomposes, finishes Task A, fails
    Task B three times, and pauses at the `replan_wait` interrupt.
    `messages` stays empty for this whole path (no `initial_message`
    seeded), so `node_memory_curator`'s `if not phase_events` guard skips
    its LLM call at both the plan_wait and coder-exhaustion checkpoints --
    no curator responses are scripted here.
    """
    return FakeRouter(
        [READY_TURN, EMPTY_DRAFT, NO_FINDINGS_CRITIQUE, _TWO_TASKS, CODER_DONE]
        + _ONE_FAILED_TASK_ATTEMPT * MAX_TASK_ATTEMPTS
    )


def test_replan_interrupt_fires_survives_restart_and_resumes(
    tmp_path, memory_conn, projects_root
):
    """ADR-047 DoD: a project whose plan rests on a false assumption reaches
    replan_wait, survives a restart there, and on approval produces a new
    plan that goes through normal plan approval with done tasks preserved.

    Two tasks are scripted (_TWO_TASKS) so there is a done task to preserve
    across the replan: Task A succeeds on its first attempt, Task B exhausts
    MAX_TASK_ATTEMPTS -- see _replan_router()'s docstring for the exact
    35-response script/order that drives the project to the replan_wait
    interrupt in two `.invoke()` calls.

    After the simulated restart, resuming with "approve" replays
    node_replan_wait's post-interrupt body (increments replan_count,
    injects the failure context into messages) then flows through
    memory_curator -- now a real call, since messages is non-empty for the
    first time on this thread -- to planner_draft/planner_critique for a
    redraft, pausing again at the ordinary plan-approval gate. Scripts
    CURATION_OK for that curator call plus a fresh draft/critique pair
    (EMPTY_DRAFT/NO_FINDINGS_CRITIQUE, reused rather than duplicated content
    -- nothing here asserts on plan text).
    """
    db_path = tmp_path / "foundry_memory.db"
    project_id = "proj-replan-restart"
    config = _config(project_id)

    checkpointer_before_restart = build_sqlite_checkpointer(db_path)
    graph_before_restart = build_project_graph(
        checkpointer_before_restart,
        _replan_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )

    graph_before_restart.invoke(new_project_state(project_id), config=config)
    graph_before_restart.invoke(Command(resume="approve"), config=config)

    snapshot = graph_before_restart.get_state(config)
    assert snapshot.values["phase"] == "awaiting_replan_approval"
    assert snapshot.values["pending_approval"]["kind"] == "replan_approval"
    assert snapshot.values["pending_approval"]["trigger"] == "coder"
    assert snapshot.values["pending_approval"]["replan_count"] == 0
    assert snapshot.values["replan_count"] == 0
    assert snapshot.next == ("replan_wait",)
    assert any(task.interrupts for task in snapshot.tasks)
    assert snapshot.values["task_queue"][0]["status"] == "done"
    assert snapshot.values["task_queue"][1]["status"] == "blocked"
    assert snapshot.values["task_queue"][1]["attempts"] == MAX_TASK_ATTEMPTS

    checkpointer_before_restart.conn.close()

    checkpointer_after_restart = build_sqlite_checkpointer(db_path)
    router_after_restart = FakeRouter([CURATION_OK, EMPTY_DRAFT, NO_FINDINGS_CRITIQUE])
    graph_after_restart = build_project_graph(
        checkpointer_after_restart,
        router_after_restart,
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )

    snapshot_after_restart = graph_after_restart.get_state(config)
    assert snapshot_after_restart.values["phase"] == "awaiting_replan_approval"
    assert snapshot_after_restart.values["pending_approval"]["kind"] == "replan_approval"
    assert snapshot_after_restart.values["replan_count"] == 0
    assert snapshot_after_restart.next == ("replan_wait",)
    assert any(task.interrupts for task in snapshot_after_restart.tasks)

    graph_after_restart.invoke(Command(resume="approve"), config=config)

    final_snapshot = graph_after_restart.get_state(config)
    assert final_snapshot.values["phase"] == "awaiting_plan_approval"
    assert final_snapshot.values["pending_approval"]["kind"] == "plan_approval"
    assert final_snapshot.next == ("plan_wait",)
    assert final_snapshot.values["replan_count"] == 1
    assert final_snapshot.values["task_queue"][0]["status"] == "done"


def test_replan_reject_halts(tmp_path, memory_conn, projects_root):
    """Rejecting a proposed replan halts the run exactly like every other
    reject path in this file.

    node_replan_wait's reject branch sets phase "blocked" without touching
    messages, so memory_curator's `if not phase_events` guard skips its LLM
    call (messages stays empty for this whole path) and
    route_after_memory_curator's `phase == "blocked"` check routes straight
    to END -- no responses beyond the 35 _replan_router() scripts to drive
    the project to the replan_wait interrupt.
    """
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        _replan_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-replan-reject")

    graph.invoke(new_project_state("proj-replan-reject"), config=config)
    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_replan_approval"
    assert snapshot.next == ("replan_wait",)

    graph.invoke(Command(resume="reject"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "blocked"
    assert snapshot.values["replan_count"] == 0
    assert snapshot.next == ()


def test_third_replan_attempt_halts(tmp_path, memory_conn, projects_root):
    """At the replan cap (MAX_REPLANS), a task exhausting its attempts
    blocks outright instead of arming replan_wait.

    Seeds replan_count at the cap directly on the dict new_project_state()
    returns (a plain TypedDict, no constructor parameter for this -- the
    field only exists to be advanced by node_replan_wait, never to be
    pre-set on a real project) before the first `.invoke()`, then drives the
    identical two-task exhaustion script test_replan_interrupt_fires_
    survives_restart_and_resumes and test_replan_reject_halts use.
    node_coder's `if state["replan_count"] < MAX_REPLANS` check (graph/
    nodes.py) is false at the cap, so the exhausted attempt returns
    phase="blocked" with no `pending_approval` key in the update at all
    (leaving whatever it was already, None) rather than the replan_approval
    payload. route_after_coder still sends "blocked" to memory_curator
    (whose LLM call is again skipped -- messages stays empty), and
    route_after_memory_curator's `phase == "blocked"` check halts with END
    -- replan_wait is never entered.
    """
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer,
        _replan_router(),
        _UNUSED_EXECUTOR_CLIENT,
        memory_conn,
        projects_root,
        StacksConfig(stacks={}),
    )
    config = _config("proj-replan-cap")

    initial_state = new_project_state("proj-replan-cap")
    initial_state["replan_count"] = MAX_REPLANS

    graph.invoke(initial_state, config=config)
    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "blocked"
    assert snapshot.values.get("pending_approval") is None
    assert snapshot.values["replan_count"] == MAX_REPLANS
    assert snapshot.next == ()
