"""Unit tests for node bodies and gate/routing functions.

`node_planner_wait`, `node_cost_wait`, `node_merge_wait`, and `node_plan_wait`
call `interrupt()`, which relies on LangGraph's runtime context and can't be
exercised by calling the bare function directly -- they're covered end to
end in `test_graph_end_to_end.py` instead. `node_coder` never calls
`interrupt()` at all, so it's fully testable directly like any other node.
`node_replan_wait`'s tests below take a third approach: `interrupt` is
imported by name into `graph.nodes`' module namespace, so monkeypatching
`services.orchestrator.graph.nodes.interrupt` to a plain stub sidesteps the
runtime-context requirement entirely and lets the bare function run to
completion, exercising both branches directly without a real graph.
"""

from langchain_core.messages import AIMessage

from services.orchestrator.agents.coder import CoderTask, _TaskList
from services.orchestrator.agents.frontend_reviewer import FrontendFinding, FrontendReviewResult
from services.orchestrator.agents.planner import InterviewTurn
from services.orchestrator.agents.reviewer import ReviewFinding, ReviewResult
from services.orchestrator.executor_client import (
    CommandResult,
    ExecutorClientError,
    FileEntry,
    ScreenshotResult,
)
from services.orchestrator.executor_client import TestResult as ExecutorTestResult
from services.orchestrator.graph.nodes import (
    MAX_REPLANS,
    MAX_TASK_ATTEMPTS,
    _reopen_last_done_task_or_block,
    node_coder,
    node_done,
    node_frontend_review,
    node_intake,
    node_memory_curator,
    node_planner_ask,
    node_planner_critique,
    node_planner_draft,
    node_replan_wait,
    node_reviewer,
    node_static_checks,
    node_task_decomposition,
    node_testing,
    route_after_coder,
    route_after_cost_check,
    route_after_frontend_review,
    route_after_memory_curator,
    route_after_plan_wait,
    route_after_planner_ask,
    route_after_replan_wait,
    route_after_reviewer,
    route_after_static_checks,
    route_after_testing,
)
from services.orchestrator.graph.nodes import node_cost_check as cost_check
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import (
    CODER_DONE,
    EMPTY_DRAFT,
    NO_FINDINGS_CRITIQUE,
    ONE_TASK,
    READY_TURN,
    FakeRouter,
    structured,
)
from services.orchestrator.progress_buffer import get_chunks_since


def _tool_call(name: str, args: dict, call_id: str = "call_1"):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
        usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
    )


def _write_then_done(path: str) -> list:
    return [_tool_call("write_file", {"path": path, "content": "x"}), CODER_DONE]


class _RecordingExecutorClient:
    """Minimal fake matching ExecutorClient's async surface, recording calls."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.calls: list[tuple] = []
        self.test_result: ExecutorTestResult | None = None
        self.screenshot_results: list[ScreenshotResult] | None = None

    async def read_file(self, project_id, path, stack: str = "python"):
        self.calls.append(("read_file", project_id, path))
        if path not in self.files:
            raise ExecutorClientError(f"404: '{path}' not found")
        return self.files[path]

    async def write_file(self, project_id, path, content, stack: str = "python"):
        self.calls.append(("write_file", project_id, path, content))
        self.files[path] = content
        return True

    async def list_files(self, project_id, path=".", stack: str = "python"):
        self.calls.append(("list_files", project_id, path))
        return [FileEntry(name=name, is_dir=False) for name in self.files]

    async def run_command(self, project_id, command, timeout=None, stack: str = "python"):
        self.calls.append(("run_command", project_id, tuple(command)))
        if tuple(command[:3]) == ("git", "diff", "--cached"):
            return CommandResult(stdout="", stderr="", exit_code=1)  # has changes
        return CommandResult(stdout="", stderr="", exit_code=0)

    async def run_tests(self, project_id: str, framework: str = "pytest", stack: str = "python"):
        self.calls.append(("run_tests", project_id, framework))
        if self.test_result is not None:
            return self.test_result
        # Default: all tests pass with 100% coverage
        return ExecutorTestResult(passed=3, failed=0, coverage_pct=100.0, report="")

    async def take_screenshot(self, project_id, url="http://localhost:5173", **kwargs):
        self.calls.append(("take_screenshot", project_id, url, kwargs))
        if self.screenshot_results:
            return self.screenshot_results.pop(0)
        return ScreenshotResult(image_b64="iVBORw0KGgo=", error=None)


def test_node_intake_advances_to_planning():
    state = new_project_state("proj-1")

    assert node_intake(state) == {"phase": "planning"}


def test_node_planner_ask_arms_the_question_gate_when_more_info_needed():
    router = FakeRouter([structured(InterviewTurn(status="questions", questions=["What language?"]))])
    state = new_project_state("proj-1", initial_message="build a thing")

    update = node_planner_ask(state, router)

    assert update["phase"] == "planning"
    assert update["pending_approval"] == {
        "kind": "plan_questions",
        "thread_id": "proj-1",
        "project_id": "proj-1",
        "questions": ["What language?"],
        "summary": "- What language?",
    }
    assert update["messages"][0].content == "- What language?"
    assert update["cost_usd_session"] > 0


def test_node_planner_ask_advances_to_roasting_when_ready():
    router = FakeRouter([READY_TURN])
    state = new_project_state("proj-1", initial_message="build a thing")

    update = node_planner_ask(state, router)

    assert update["phase"] == "roasting"
    assert update["pending_approval"] is None
    assert update["cost_usd_session"] > 0


def test_extract_description_and_history_uses_summary_and_cursor_when_set():
    from services.orchestrator.graph.nodes import _extract_description_and_history
    from langchain_core.messages import HumanMessage

    state = new_project_state("proj-1")
    state["messages"] = [
        HumanMessage(content="original request"),
        HumanMessage(content="old question"),
        HumanMessage(content="old answer"),
        HumanMessage(content="new question"),
        HumanMessage(content="new answer"),
    ]
    state["messages_summary"] = "Stakeholder already answered the basics."
    state["messages_curated_through"] = 3

    description, qa_history = _extract_description_and_history(state)

    assert description == "original request"
    assert qa_history == [
        "Stakeholder already answered the basics.",
        "new question",
        "new answer",
    ]


def test_extract_description_and_history_omits_summary_when_never_curated():
    from services.orchestrator.graph.nodes import _extract_description_and_history

    state = new_project_state("proj-1", initial_message="original request")

    description, qa_history = _extract_description_and_history(state)

    assert description == "original request"
    assert qa_history == []


def test_route_after_planner_ask_waits_when_questioning():
    state = new_project_state("proj-1")
    state["phase"] = "planning"

    assert route_after_planner_ask(state) == "planner_wait"


def test_route_after_planner_ask_drafts_when_ready():
    state = new_project_state("proj-1")
    state["phase"] = "roasting"

    assert route_after_planner_ask(state) == "planner_draft"


def test_node_planner_draft_produces_a_plan():
    router = FakeRouter([EMPTY_DRAFT])
    state = new_project_state("proj-1", initial_message="build a thing")

    update = node_planner_draft(state, router)

    assert update["phase"] == "roasting"
    assert update["plan"]["summary"] == "A minimal test project."


def test_node_planner_critique_arms_the_plan_approval_gate():
    router = FakeRouter([NO_FINDINGS_CRITIQUE])
    state = new_project_state("proj-1")
    state["plan"] = {"summary": "A minimal test project.", "risks": [], "assumptions": []}

    update = node_planner_critique(state, router)

    assert update["phase"] == "awaiting_plan_approval"
    assert update["pending_approval"]["kind"] == "plan_approval"
    assert update["pending_approval"]["critique_findings"] == []
    assert update["plan"]["summary"] == "A minimal test project."


def test_route_after_plan_wait_always_advances_to_memory_curator_even_when_blocked():
    """Blocked is a curation-worthy transition too (Phase 14) -- route_after_memory_curator,
    not this function, is the one that actually halts with END."""
    state = new_project_state("proj-1")
    state["phase"] = "blocked"

    assert route_after_plan_wait(state) == "memory_curator"


def test_route_after_plan_wait_advances_to_memory_curator_when_approved():
    state = new_project_state("proj-1")
    state["phase"] = "implementing"

    assert route_after_plan_wait(state) == "memory_curator"


def test_node_task_decomposition_populates_task_queue():
    router = FakeRouter([ONE_TASK])
    state = new_project_state("proj-1")
    state["plan"] = {"summary": "A minimal test project."}

    update = node_task_decomposition(state, router)

    assert update["phase"] == "implementing"
    assert update["task_queue"] == [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]


def test_task_decomposition_preserves_done_tasks():
    """On a replan (ADR-047), done tasks from the failed plan are carried
    forward and everything else (blocked/pending) is dropped in favor of
    the redrafted plan's fresh decomposition."""
    two_new_tasks = structured(
        _TaskList(
            tasks=[
                CoderTask(description="New task A"),
                CoderTask(description="New task B"),
            ]
        )
    )
    router = FakeRouter([two_new_tasks])
    state = new_project_state("proj-1")
    state["plan"] = {"summary": "A corrected test project."}
    state["task_queue"] = [
        {"description": "keep me", "status": "done", "attempts": 1},
        {"description": "drop me", "status": "blocked", "attempts": 3},
    ]

    update = node_task_decomposition(state, router)

    assert update["task_queue"][0] == {
        "description": "keep me", "status": "done", "attempts": 1,
    }
    assert update["task_queue"][1:] == [
        {"description": "New task A", "status": "pending", "attempts": 0},
        {"description": "New task B", "status": "pending", "attempts": 0},
    ]
    assert not any(task["description"] == "drop me" for task in update["task_queue"])


def test_node_coder_returns_reviewing_phase_when_queue_already_empty():
    state = new_project_state("proj-1")
    state["task_queue"] = []

    update = node_coder(state, router=None, executor_client=None)

    assert update == {"phase": "reviewing"}


def test_node_coder_returns_reviewing_phase_when_every_task_is_already_done():
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "First task", "status": "done", "attempts": 1}
    ]

    update = node_coder(state, router=None, executor_client=None)

    assert update == {"phase": "reviewing"}


def test_node_coder_succeeds_and_advances_to_reviewing_when_last_task_done():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [
        {"description": "Do the trivial thing", "status": "done", "attempts": 0}
    ]
    assert update["phase"] == "reviewing"


def test_node_coder_keeps_implementing_when_more_tasks_remain():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "First task", "status": "pending", "attempts": 0},
        {"description": "Second task", "status": "pending", "attempts": 0},
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [
        {"description": "First task", "status": "done", "attempts": 0},
        {"description": "Second task", "status": "pending", "attempts": 0},
    ]
    assert update["phase"] == "implementing"


def test_node_coder_accumulates_changed_files_across_tasks():
    router = FakeRouter(_write_then_done("models.py") + _write_then_done("views.py"))
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "First task", "status": "pending", "attempts": 0},
        {"description": "Second task", "status": "pending", "attempts": 0},
    ]

    first_update = node_coder(state, router, executor_client=_RecordingExecutorClient())
    state.update(first_update)
    second_update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert second_update["changed_files"] == ["models.py", "views.py"]


def test_node_coder_dedupes_changed_files_across_retries():
    router = FakeRouter(_write_then_done("models.py"))
    state = new_project_state("proj-1")
    state["changed_files"] = ["models.py"]
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["changed_files"] == ["models.py"]


def test_node_coder_records_changed_files_even_when_task_blocks():
    """At the replan cap (ADR-047), exhaustion still blocks outright -- this
    test pins that changed_files is preserved on that path specifically."""
    router = FakeRouter(_write_then_done("partial.py")[:1] * 10)
    state = new_project_state("proj-1")
    state["replan_count"] = MAX_REPLANS
    state["task_queue"] = [
        {
            "description": "Do the trivial thing",
            "status": "in_progress",
            "attempts": MAX_TASK_ATTEMPTS - 1,
        }
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "blocked"
    assert update["changed_files"] == ["partial.py"]


def test_node_coder_resumes_the_in_progress_task_rather_than_the_first_pending_one():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "First task", "status": "done", "attempts": 0},
        {"description": "Second task", "status": "in_progress", "attempts": 1},
        {"description": "Third task", "status": "pending", "attempts": 0},
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [
        {"description": "First task", "status": "done", "attempts": 0},
        {"description": "Second task", "status": "done", "attempts": 1},
        {"description": "Third task", "status": "pending", "attempts": 0},
    ]
    assert update["phase"] == "implementing"


def test_node_coder_recalls_lessons_from_a_seeded_global_store(memory_conn):
    """Phase 14's cross-project loop, closed: a lesson recorded by project A's
    curator is recalled and fed to project B's Coder via memory_conn --
    the node completes normally with recall active."""
    from services.orchestrator.memory.global_store import record_memory

    record_memory(
        memory_conn,
        project_id="proj-a",
        type_="fix",
        content="Escape quotes before writing YAML values.",
        tags="yaml quoting",
        created_at="2026-07-01T00:00:00Z",
    )
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-b")
    state["task_queue"] = [
        {"description": "Fix the yaml quoting bug", "status": "pending", "attempts": 0}
    ]

    update = node_coder(
        state, router, executor_client=_RecordingExecutorClient(), memory_conn=memory_conn
    )

    assert update["phase"] == "reviewing"
    assert update["task_queue"][0]["status"] == "done"


def test_node_coder_retries_a_failed_task_below_the_attempt_limit():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "implementing"
    assert update["task_queue"] == [
        {"description": "Do the trivial thing", "status": "in_progress", "attempts": 1}
    ]


def test_node_coder_records_a_progress_event_when_a_task_succeeds():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-progress-success")
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    node_coder(state, router, executor_client=_RecordingExecutorClient())

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-success")]
    assert "✓ task succeeded (0 remaining)" in chunks


def test_node_coder_records_a_progress_event_on_retry():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-progress-retry")
    state["task_queue"] = [
        {"description": "Do the trivial thing", "status": "pending", "attempts": 0}
    ]

    node_coder(state, router, executor_client=_RecordingExecutorClient())

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-retry")]
    assert "↻ task attempt 1 failed, retrying" in chunks


def test_node_coder_records_a_progress_event_when_blocked_at_the_replan_cap():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-progress-blocked")
    state["replan_count"] = MAX_REPLANS
    state["task_queue"] = [
        {
            "description": "Do the trivial thing",
            "status": "in_progress",
            "attempts": MAX_TASK_ATTEMPTS - 1,
        }
    ]

    node_coder(state, router, executor_client=_RecordingExecutorClient())

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-blocked")]
    assert f"✗ task blocked after {MAX_TASK_ATTEMPTS} attempts" in chunks


def test_node_coder_records_a_progress_event_when_proposing_a_replan():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-progress-replan")
    state["replan_count"] = 0
    state["task_queue"] = [
        {
            "description": "Do the trivial thing",
            "status": "in_progress",
            "attempts": MAX_TASK_ATTEMPTS - 1,
        }
    ]

    node_coder(state, router, executor_client=_RecordingExecutorClient())

    chunks = [chunk.text for chunk in get_chunks_since("proj-progress-replan")]
    assert f"↻ task exhausted {MAX_TASK_ATTEMPTS} attempts, proposing replan" in chunks


def test_node_coder_blocks_after_exhausting_the_attempt_budget():
    """At the replan cap (ADR-047), exhaustion blocks outright, same as
    before this phase; the under-cap case is covered separately by
    test_coder_exhaustion_routes_to_replan_when_under_cap."""
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["replan_count"] = MAX_REPLANS
    state["task_queue"] = [
        {
            "description": "Do the trivial thing",
            "status": "in_progress",
            "attempts": MAX_TASK_ATTEMPTS - 1,
        }
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "blocked"
    assert update["curator_source"] == "coder"
    assert update["task_queue"] == [
        {
            "description": "Do the trivial thing",
            "status": "blocked",
            "attempts": MAX_TASK_ATTEMPTS,
        }
    ]


def test_route_after_coder_routes_to_memory_curator_when_blocked():
    """Exhausted retries are a curation-worthy transition into blocked (Phase 14)."""
    state = new_project_state("proj-1")
    state["phase"] = "blocked"

    assert route_after_coder(state) == "memory_curator"


def test_route_after_coder_loops_back_when_still_implementing():
    state = new_project_state("proj-1")
    state["phase"] = "implementing"

    assert route_after_coder(state) == "coder"


def test_route_after_coder_sends_reviewing_to_static_checks():
    state = new_project_state("proj-1")
    state["phase"] = "reviewing"

    assert route_after_coder(state) == "static_checks"


def test_route_after_coder_sends_replan_proposal_to_memory_curator():
    state = new_project_state("proj-1")
    state["phase"] = "awaiting_replan_approval"

    assert route_after_coder(state) == "memory_curator"


# -- Replan trigger tests (ADR-047) ---------------------------------------------


def test_coder_exhaustion_routes_to_replan_when_under_cap():
    """Third failed attempt: task blocked, but phase becomes
    awaiting_replan_approval with a replan payload, not a terminal halt."""
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["replan_count"] = 0
    state["task_queue"] = [
        {
            "description": "Do the trivial thing",
            "status": "in_progress",
            "attempts": MAX_TASK_ATTEMPTS - 1,
        }
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "awaiting_replan_approval"
    assert update["task_queue"][0]["status"] == "blocked"
    assert update["curator_source"] == "coder"
    assert update["pending_approval"]["kind"] == "replan_approval"
    assert "summary" in update["pending_approval"]


def test_coder_exhaustion_at_replan_cap_blocks_as_before():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["replan_count"] = MAX_REPLANS
    state["task_queue"] = [
        {
            "description": "Do the trivial thing",
            "status": "in_progress",
            "attempts": MAX_TASK_ATTEMPTS - 1,
        }
    ]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "blocked"
    assert update.get("pending_approval") is None


# -- Static checks node tests (ADR-046) -----------------------------------------


class _StaticChecksExecutorClient:
    """Minimal fake for node_static_checks tests: every run_command call
    returns the same canned exit code/stdout regardless of the command,
    since node_static_checks itself has no per-command branching -- that
    logic lives in and is already covered by agents/static_checks.py's own
    unit tests."""

    def __init__(self, default_exit_code: int = 0, default_stdout: str = ""):
        self.default_exit_code = default_exit_code
        self.default_stdout = default_stdout
        self.calls: list[tuple] = []

    async def run_command(self, project_id, command, timeout=None, stack: str = "python"):
        self.calls.append(("run_command", project_id, tuple(command)))
        return CommandResult(
            stdout=self.default_stdout, stderr="", exit_code=self.default_exit_code
        )


def _checked_state(task_queue, stack="python"):
    state = new_project_state("proj-1")
    state["phase"] = "reviewing"
    state["stack"] = stack
    state["task_queue"] = task_queue
    return state


def _passing_checks_client():
    return _StaticChecksExecutorClient(default_exit_code=0)


def _failing_checks_client(output="app.py:1:1: F821 undefined name"):
    return _StaticChecksExecutorClient(default_exit_code=1, default_stdout=output)


def test_static_checks_pass_keeps_reviewing_phase(two_stack_registry):
    state = _checked_state([{"description": "t", "status": "done", "attempts": 1}])

    update = node_static_checks(
        state, executor_client=_passing_checks_client(), stacks_config=two_stack_registry
    )

    assert update == {}


def test_static_checks_failure_reopens_last_done_task_with_feedback(two_stack_registry):
    queue = [
        {"description": "first", "status": "done", "attempts": 1},
        {"description": "second", "status": "done", "attempts": 0},
    ]
    state = _checked_state(queue)

    update = node_static_checks(
        state, executor_client=_failing_checks_client(), stacks_config=two_stack_registry
    )

    assert update["phase"] == "implementing"
    assert update["task_queue"][1]["status"] == "in_progress"
    assert update["task_queue"][1]["attempts"] == 1
    assert update["task_queue"][0] == queue[0]
    assert "F821" in update["messages"][0].content


def test_static_checks_exhaustion_blocks(two_stack_registry):
    queue = [{"description": "t", "status": "done", "attempts": MAX_TASK_ATTEMPTS - 1}]
    state = _checked_state(queue)

    update = node_static_checks(
        state, executor_client=_failing_checks_client(), stacks_config=two_stack_registry
    )

    assert update["phase"] == "blocked"
    assert update["task_queue"][0]["status"] == "blocked"
    assert update["task_queue"][0]["attempts"] == MAX_TASK_ATTEMPTS
    assert update["curator_source"] == "static_checks"


def test_static_checks_failure_with_no_done_task_blocks(two_stack_registry):
    state = _checked_state([])

    update = node_static_checks(
        state, executor_client=_failing_checks_client(), stacks_config=two_stack_registry
    )

    assert update["phase"] == "blocked"
    assert update["curator_source"] == "static_checks"
    assert "task_queue" not in update
    assert "F821" in update["messages"][0].content


def test_static_checks_pass_with_nothing_applicable_keeps_reviewing_phase(two_stack_registry):
    state = _checked_state(
        [{"description": "t", "status": "done", "attempts": 0}], stack="node"
    )

    update = node_static_checks(
        state, executor_client=_passing_checks_client(), stacks_config=two_stack_registry
    )

    assert update == {}


def test_reopen_last_done_task_or_block_reopens_the_last_done_task():
    state = _checked_state([{"description": "t1", "status": "done", "attempts": 0}])

    update = _reopen_last_done_task_or_block(
        state, AIMessage(content="feedback"), "static_checks"
    )

    assert update["phase"] == "implementing"
    assert update["task_queue"][0]["status"] == "in_progress"
    assert update["task_queue"][0]["attempts"] == 1


def test_reopen_last_done_task_or_block_blocks_at_max_attempts():
    state = _checked_state(
        [{"description": "t1", "status": "done", "attempts": MAX_TASK_ATTEMPTS - 1}]
    )

    update = _reopen_last_done_task_or_block(
        state, AIMessage(content="feedback"), "static_checks"
    )

    assert update["phase"] == "blocked"
    assert update["task_queue"][0]["status"] == "blocked"


def test_reopen_last_done_task_or_block_blocks_when_no_done_task_exists():
    state = _checked_state([])

    update = _reopen_last_done_task_or_block(
        state, AIMessage(content="feedback"), "static_checks"
    )

    assert update["phase"] == "blocked"
    assert "task_queue" not in update


def test_route_after_static_checks_routes_all_three_outcomes():
    state = new_project_state("proj-1")

    state["phase"] = "reviewing"
    assert route_after_static_checks(state) == "reviewer"

    state["phase"] = "implementing"
    assert route_after_static_checks(state) == "coder"

    state["phase"] = "blocked"
    assert route_after_static_checks(state) == "memory_curator"


def test_route_after_testing_routes_failure_to_memory_curator_and_pass_to_after_tester():
    state = new_project_state("proj-1")

    state["phase"] = "implementing"
    assert route_after_testing(state) == "memory_curator"

    state["phase"] = "awaiting_merge_approval"
    assert route_after_testing(state) == "after_tester"


class _FakeCheckpointer:
    def __init__(self, others: dict[str, float]):
        self._others = others

    def list(self, config):
        for thread_id in self._others:
            yield type(
                "T", (), {"config": {"configurable": {"thread_id": thread_id}}}
            )()

    def get_tuple(self, config):
        thread_id = config["configurable"]["thread_id"]
        if thread_id not in self._others:
            return None
        return type(
            "T",
            (),
            {"checkpoint": {"channel_values": {"cost_usd_weekly": self._others[thread_id]}}},
        )()


def test_node_cost_check_returns_empty_update_below_global_threshold():
    state = new_project_state("proj-1", cost_usd_weekly=10.0)
    checkpointer = _FakeCheckpointer({"proj-2": 20.0})  # 10 + 20 = 30, under 50

    assert cost_check(state, checkpointer=checkpointer, threshold_usd=50.0) == {}


def test_node_cost_check_arms_gate_when_global_total_crosses_threshold():
    state = new_project_state("proj-1", cost_usd_weekly=10.0)
    checkpointer = _FakeCheckpointer({"proj-2": 45.0})  # 10 + 45 = 55, over 50

    update = cost_check(state, checkpointer=checkpointer, threshold_usd=50.0)

    assert update["phase"] == "awaiting_cost_approval"
    assert update["pending_approval"] == {
        "kind": "cost_alert",
        "thread_id": "proj-1",
        "project_id": "proj-1",
        "cost_usd_weekly_global": 55.0,
        "threshold_usd": 50.0,
        "summary": "Crossed $55.00 of $50.00 global weekly cost limit -- continue?",
    }


def test_node_cost_check_trips_purely_from_other_projects_even_with_zero_own_cost():
    state = new_project_state("proj-1", cost_usd_weekly=0.0)
    checkpointer = _FakeCheckpointer({"proj-2": 60.0})

    update = cost_check(state, checkpointer=checkpointer, threshold_usd=50.0)

    assert update["phase"] == "awaiting_cost_approval"


def test_route_after_cost_check_waits_when_gate_armed():
    state = new_project_state("proj-1")
    state["phase"] = "awaiting_cost_approval"

    assert route_after_cost_check(state) == "cost_wait"


def test_route_after_cost_check_skips_wait_when_gate_not_armed():
    state = new_project_state("proj-1")
    state["phase"] = "testing"

    assert route_after_cost_check(state) == "testing"


def test_node_testing_arms_merge_gate_on_pass():
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(passed=3, failed=0, coverage_pct=95.0, report="")
    state = new_project_state("proj-1", pr_mode="manual")

    update = node_testing(state, executor)

    assert update["phase"] == "awaiting_merge_approval"
    assert update["curator_source"] == "testing"
    assert update["coverage_pct"] == 95.0
    assert update["pending_approval"]["kind"] == "merge_approval"


def test_node_testing_routes_back_to_coder_on_test_failure():
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(
        passed=2, failed=1, coverage_pct=95.0,
        report="FAILED tests/test_models.py::test_user_creation - AssertionError",
    )
    state = new_project_state("proj-1")

    update = node_testing(state, executor)

    assert update["phase"] == "implementing"
    assert update["curator_source"] == "testing"
    assert len(update["messages"]) == 1
    assert "Test failures:" in update["messages"][0].content


def test_node_testing_reports_collection_errors_without_crashing():
    """A collection/import error yields failed>0 with no FAILED lines and no
    coverage percentage -- the feedback path must not assume either exists."""
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(
        passed=0, failed=2, coverage_pct=None,
        report="ERROR tests/test_x.py - ImportError: boom\n2 errors",
    )
    state = new_project_state("proj-1")

    update = node_testing(state, executor)

    assert update["phase"] == "implementing"
    assert update["curator_source"] == "testing"
    assert "no parseable FAILED lines" in update["messages"][0].content
    assert "ImportError: boom" in update["messages"][0].content


def test_node_testing_coverage_failure_attaches_uncovered_lines():
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(
        passed=3, failed=0, coverage_pct=50.0,
        report=(
            "Name                  Stmts   Miss  Cover   Missing\n"
            "services/foo/bar.py      20     10    50%   5-14\n"
            "TOTAL                    20     10    50%\n"
        ),
    )
    state = new_project_state("proj-1", coverage_gate_mode="hard", coverage_threshold=80)

    update = node_testing(state, executor)

    assert update["phase"] == "implementing"
    content = update["messages"][0].content
    assert "Uncovered lines:" in content
    assert "services/foo/bar.py: 5-14" in content


def test_node_testing_hard_mode_blocks_on_low_coverage():
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(passed=3, failed=0, coverage_pct=50.0, report="")
    state = new_project_state(
        "proj-1", coverage_gate_mode="hard", coverage_threshold=80
    )

    update = node_testing(state, executor)

    assert update["phase"] == "implementing"
    assert update["coverage_pct"] == 50.0


def test_node_testing_soft_mode_passes_on_low_coverage():
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(passed=3, failed=0, coverage_pct=50.0, report="")
    state = new_project_state(
        "proj-1", coverage_gate_mode="soft", coverage_threshold=80
    )

    update = node_testing(state, executor)

    assert update["phase"] == "awaiting_merge_approval"
    assert update["coverage_pct"] == 50.0


def test_node_testing_defaults_to_hard_mode_for_a_new_project():
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(passed=3, failed=0, coverage_pct=50.0, report="")
    state = new_project_state("proj-1", coverage_threshold=80)

    update = node_testing(state, executor)

    assert update["phase"] == "implementing"


def test_node_testing_gate_mode_is_independent_of_pr_mode():
    """pr_mode governs GitHub PR automation, not coverage strictness (ADR-023)."""
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(passed=3, failed=0, coverage_pct=50.0, report="")
    state = new_project_state(
        "proj-1", pr_mode="auto", coverage_gate_mode="soft", coverage_threshold=80
    )

    update = node_testing(state, executor)

    assert update["phase"] == "awaiting_merge_approval"


def test_node_testing_records_coverage_on_pass():
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(passed=3, failed=0, coverage_pct=87.5, report="")
    state = new_project_state("proj-1", pr_mode="manual")

    update = node_testing(state, executor)

    assert update["coverage_pct"] == 87.5


def test_node_testing_calls_executor_run_tests():
    executor = _RecordingExecutorClient()
    executor.test_result = ExecutorTestResult(passed=3, failed=0, coverage_pct=100.0, report="")
    state = new_project_state("proj-1", pr_mode="manual")

    node_testing(state, executor)

    assert ("run_tests", "proj-1", "pytest") in executor.calls


# -- node_done tests (Phase 13: always commits locally, autonomously) -----------


def test_node_done_commits_locally_when_no_remote_is_configured():
    executor = _RecordingExecutorClient()
    state = new_project_state("proj-1")
    state["phase"] = "done"

    update = node_done(state, executor)

    assert update == {"phase": "done"}
    cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
    assert any(c[2][:2] == ("git", "add") for c in cmd_calls)
    assert any(c[2][:2] == ("git", "commit") for c in cmd_calls)
    assert not any(c[2][:2] == ("git", "push") for c in cmd_calls)


def test_node_done_derives_branch_name_from_project_id_when_unset():
    executor = _RecordingExecutorClient()
    state = new_project_state("proj-xyz")
    state["phase"] = "done"

    node_done(state, executor)

    checkout_calls = [c for c in executor.calls if c[0] == "run_command" and c[2][:2] == ("git", "checkout")]
    assert checkout_calls
    assert "feature/proj-xyz" in checkout_calls[0][2]


def test_node_done_pushes_when_a_remote_is_configured():
    executor = _RecordingExecutorClient()
    state = new_project_state("proj-1")
    state["phase"] = "done"
    state["git_remote_url"] = "git@github.com:test/repo.git"
    state["pr_mode"] = "manual"

    update = node_done(state, executor)

    assert update == {"phase": "done"}
    cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
    assert any(c[2][:2] == ("git", "push") for c in cmd_calls)


# -- memory_curator wiring smoke test (full coverage in test_memory_curator.py) --


def test_node_memory_curator_is_a_noop_without_a_curator_source():
    state = new_project_state("proj-1")

    assert node_memory_curator(state, projects_root=None, conn=None, router=None) == {}


def test_task_progress_lines_renders_each_task_with_status_and_attempts():
    from services.orchestrator.graph.nodes import _task_progress_lines

    task_queue = [
        {"description": "Add a User model", "status": "done", "attempts": 1},
        {"description": "Add a POST /login endpoint", "status": "blocked", "attempts": 3},
    ]

    lines = _task_progress_lines(task_queue)

    assert lines == [
        "[done] Add a User model (attempts: 1)",
        "[blocked] Add a POST /login endpoint (attempts: 3)",
    ]


def test_task_progress_lines_returns_empty_list_for_empty_queue():
    from services.orchestrator.graph.nodes import _task_progress_lines

    assert _task_progress_lines([]) == []


def test_task_progress_lines_caps_done_tasks_to_the_most_recent_limit():
    from services.orchestrator.graph.nodes import _task_progress_lines

    task_queue = (
        [{"description": f"Old done task {i}", "status": "done", "attempts": 0} for i in range(15)]
        + [{"description": "Still pending", "status": "pending", "attempts": 0}]
    )

    lines = _task_progress_lines(task_queue, recent_done_limit=10)

    assert len(lines) == 11
    assert lines[:10] == [
        f"[done] Old done task {i} (attempts: 0)" for i in range(5, 15)
    ]
    assert lines[10] == "[pending] Still pending (attempts: 0)"


# -- Reviewer node tests -------------------------------------------------------

_APPROVE_REVIEW = structured(ReviewResult(verdict="approve", findings=[]))

_CHANGES_REQUESTED_REVIEW = structured(
    ReviewResult(
        verdict="changes-requested",
        findings=[
            ReviewFinding(
                file="models.py", line=15, severity="critical",
                message="hardcoded password in source code",
            )
        ],
    )
)


def test_node_reviewer_approves_and_advances_to_testing():
    router = FakeRouter([_APPROVE_REVIEW])
    state = new_project_state("proj-1")

    update = node_reviewer(state, router, _RecordingExecutorClient(files={"models.py": "x = 1"}))

    assert update["phase"] == "testing"
    assert update["cost_usd_session"] > 0


def test_node_reviewer_requests_changes_and_routes_back_to_coder():
    router = FakeRouter([_CHANGES_REQUESTED_REVIEW])
    state = new_project_state("proj-1")

    update = node_reviewer(state, router, _RecordingExecutorClient(files={"models.py": "x = 1"}))

    assert update["phase"] == "implementing"
    assert len(update["messages"]) == 1
    assert "Reviewer feedback:" in update["messages"][0].content
    assert "models.py:15 [critical] hardcoded password in source code" in update["messages"][0].content


def test_node_reviewer_reads_only_changed_files_when_populated():
    router = FakeRouter([_APPROVE_REVIEW])
    state = new_project_state("proj-1")
    state["changed_files"] = ["models.py"]
    executor = _RecordingExecutorClient(
        files={"models.py": "class User: pass", "unrelated.py": "x = 1"}
    )

    node_reviewer(state, router, executor)

    assert executor.calls == [("read_file", "proj-1", "models.py")]


def test_route_after_reviewer_advances_to_frontend_review_when_approved():
    state = new_project_state("proj-1")
    state["phase"] = "testing"

    assert route_after_reviewer(state) == "frontend_review"


def test_route_after_reviewer_loops_back_to_coder_on_changes_requested():
    state = new_project_state("proj-1")
    state["phase"] = "implementing"

    assert route_after_reviewer(state) == "coder"


def test_route_after_reviewer_sends_replan_to_curator():
    state = new_project_state("proj-1")
    state["phase"] = "awaiting_replan_approval"

    assert route_after_reviewer(state) == "memory_curator"


# -- Reviewer replan trigger tests (ADR-047) ------------------------------------

_REPLAN_REVIEW = structured(
    ReviewResult(
        verdict="changes-requested",
        findings=[],
        replan_needed=True,
        replan_reason="false assumption",
    )
)


def test_reviewer_replan_needed_arms_replan_gate():
    router = FakeRouter([_REPLAN_REVIEW])
    state = new_project_state("proj-1")
    state["replan_count"] = 0

    update = node_reviewer(state, router, _RecordingExecutorClient(files={"models.py": "x = 1"}))

    assert update["phase"] == "awaiting_replan_approval"
    assert update["curator_source"] == "reviewer"
    assert update["pending_approval"]["kind"] == "replan_approval"
    assert "false assumption" in update["pending_approval"]["summary"]


def test_reviewer_replan_needed_at_cap_degrades_to_changes_requested():
    router = FakeRouter([_REPLAN_REVIEW])
    state = new_project_state("proj-1")
    state["replan_count"] = MAX_REPLANS

    update = node_reviewer(state, router, _RecordingExecutorClient(files={"models.py": "x = 1"}))

    assert update["phase"] == "implementing"
    assert "false assumption" in update["messages"][0].content


# -- Frontend Reviewer node tests -----------------------------------------------

_APPROVE_FRONTEND = structured(FrontendReviewResult(verdict="approve", findings=[]))

_FRONTEND_CHANGES_REQUESTED = structured(
    FrontendReviewResult(
        verdict="changes-requested",
        findings=[
            FrontendFinding(line=5, severity="critical", message="text unreadable on dark background")
        ],
    )
)


def test_node_frontend_review_skips_when_no_frontend_files():
    router = FakeRouter([])
    executor = _RecordingExecutorClient(files={"main.py": "print('hi')"})
    state = new_project_state("proj-1")

    update = node_frontend_review(state, router, executor)

    assert update == {"phase": "testing"}
    assert not any(call[0] == "take_screenshot" for call in executor.calls)


def test_node_frontend_review_approves_and_advances_to_testing():
    router = FakeRouter([_APPROVE_FRONTEND])
    executor = _RecordingExecutorClient(files={"index.html": "<html></html>"})

    update = node_frontend_review(new_project_state("proj-1"), router, executor)

    assert update["phase"] == "testing"
    assert update["cost_usd_session"] > 0


def test_node_frontend_review_requests_changes_and_routes_back_to_coder():
    router = FakeRouter([_FRONTEND_CHANGES_REQUESTED])
    executor = _RecordingExecutorClient(files={"index.html": "<html></html>"})

    update = node_frontend_review(new_project_state("proj-1"), router, executor)

    assert update["phase"] == "implementing"
    assert len(update["messages"]) == 1
    content = update["messages"][0].content
    assert "text unreadable on dark background" in content
    assert content.count("Frontend review feedback:") == 1


def test_route_after_frontend_review_advances_to_cost_check_when_approved():
    state = new_project_state("proj-1")
    state["phase"] = "testing"

    assert route_after_frontend_review(state) == "cost_check"


def test_route_after_frontend_review_loops_back_to_coder_on_changes_requested():
    state = new_project_state("proj-1")
    state["phase"] = "implementing"

    assert route_after_frontend_review(state) == "coder"


# -- node_replan_wait / route_after_replan_wait tests (ADR-047) -----------------


def _replan_pending_approval() -> dict:
    return {
        "kind": "replan_approval",
        "thread_id": "proj-1",
        "project_id": "proj-1",
        "trigger": "coder",
        "reason": "task 'Do the trivial thing' failed 3 attempts",
        "replan_count": 0,
        "summary": "Plan appears broken (coder): task failed -- approve a replan? "
        "(0/2 replans used)",
    }


def test_replan_wait_approve_returns_to_planning_and_increments_count(monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.graph.nodes.interrupt", lambda payload: "approve"
    )
    state = new_project_state("proj-1")
    state["replan_count"] = 0
    state["pending_approval"] = _replan_pending_approval()

    update = node_replan_wait(state)

    assert update["phase"] == "planning"
    assert update["replan_count"] == 1
    assert update["curator_source"] == "replan_wait"
    assert update["pending_approval"] is None
    assert "replan" in update["messages"][0].content.lower()


def test_replan_wait_reject_blocks(monkeypatch):
    monkeypatch.setattr(
        "services.orchestrator.graph.nodes.interrupt", lambda payload: "reject"
    )
    state = new_project_state("proj-1")
    state["pending_approval"] = _replan_pending_approval()

    update = node_replan_wait(state)

    assert update["phase"] == "blocked"
    assert update["curator_source"] == "replan_wait"
    assert update["pending_approval"] is None


def test_route_after_replan_wait_always_advances_to_memory_curator():
    state = new_project_state("proj-1")
    state["phase"] = "blocked"

    assert route_after_replan_wait(state) == "memory_curator"


# -- route_after_memory_curator replan routes (ADR-047) -------------------------


def test_route_after_memory_curator_replan_routes():
    state = new_project_state("proj-1")
    state["phase"] = "awaiting_replan_approval"
    state["curator_source"] = "coder"

    assert route_after_memory_curator(state) == "replan_wait"

    state["phase"] = "planning"
    state["curator_source"] = "replan_wait"

    assert route_after_memory_curator(state) == "planner_draft"
