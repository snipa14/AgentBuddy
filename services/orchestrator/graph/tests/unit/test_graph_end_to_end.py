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
from langgraph.types import Command

from services.orchestrator.agents.planner import InterviewTurn
from services.orchestrator.executor_client import CommandResult, TestResult
from services.orchestrator.graph.build import DEFAULT_GLOBAL_COST_THRESHOLD_USD, build_project_graph
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import (
    CODER_DONE,
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
