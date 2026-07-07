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

from langgraph.types import Command

from services.orchestrator.agents.planner import InterviewTurn
from services.orchestrator.executor_client import CommandResult, TestResult
from services.orchestrator.graph.build import DEFAULT_GLOBAL_COST_THRESHOLD_USD, build_project_graph
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import (
    EMPTY_DRAFT,
    NO_FINDINGS_CRITIQUE,
    READY_TURN,
    FakeRouter,
    fast_track_router,
    structured,
)


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
    graph = build_project_graph(checkpointer, router, _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root)
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
    graph = build_project_graph(checkpointer, fast_track_router(), _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root)
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
    graph = build_project_graph(checkpointer, fast_track_router(), _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root)
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
    graph = build_project_graph(checkpointer, fast_track_router(), _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root)
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
    graph = build_project_graph(checkpointer, fast_track_router(), _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root)
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
    graph = build_project_graph(checkpointer, fast_track_router(), _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root)
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
    graph = build_project_graph(checkpointer, fast_track_router(), _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root)
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
        checkpointer_before_restart, fast_track_router(), _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root
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
        checkpointer_after_restart, FakeRouter([]), _UNUSED_EXECUTOR_CLIENT, memory_conn, projects_root
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
        global_cost_threshold_usd=1.0,
    )
    config = _config("proj-tiny-budget")

    graph.invoke(new_project_state("proj-tiny-budget", cost_usd_weekly=2.0), config=config)
    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_cost_approval"
    assert snapshot.values["pending_approval"]["threshold_usd"] == 1.0
