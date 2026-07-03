"""Unit tests for node bodies and gate/routing functions.

`node_planner_wait`, `node_cost_wait`, `node_merge_wait`, and `node_plan_wait`
call `interrupt()`, which relies on LangGraph's runtime context and can't be
exercised by calling the bare function directly -- they're covered end to
end in `test_graph_end_to_end.py` instead. `node_coder` never calls
`interrupt()` at all, so it's fully testable directly like any other node.
"""

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
    COST_THRESHOLD_USD_STUB,
    MAX_TASK_ATTEMPTS,
    node_coder,
    node_done,
    node_frontend_review,
    node_intake,
    node_memory_curator,
    node_planner_ask,
    node_planner_critique,
    node_planner_draft,
    node_reviewer,
    node_task_decomposition,
    node_testing,
    route_after_coder,
    route_after_cost_check,
    route_after_frontend_review,
    route_after_plan_wait,
    route_after_planner_ask,
    route_after_reviewer,
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


def _tool_call(name: str, args: dict, call_id: str = "call_1"):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
        usage_metadata={"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
    )


class _RecordingExecutorClient:
    """Minimal fake matching ExecutorClient's async surface, recording calls."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.calls: list[tuple] = []
        self.test_result: ExecutorTestResult | None = None
        self.screenshot_results: list[ScreenshotResult] | None = None

    async def read_file(self, project_id, path):
        self.calls.append(("read_file", project_id, path))
        if path not in self.files:
            raise ExecutorClientError(f"404: '{path}' not found")
        return self.files[path]

    async def write_file(self, project_id, path, content):
        self.calls.append(("write_file", project_id, path, content))
        self.files[path] = content
        return True

    async def list_files(self, project_id, path="."):
        self.calls.append(("list_files", project_id, path))
        return [FileEntry(name=name, is_dir=False) for name in self.files]

    async def run_command(self, project_id, command, timeout=None):
        self.calls.append(("run_command", project_id, tuple(command)))
        if tuple(command[:3]) == ("git", "diff", "--cached"):
            return CommandResult(stdout="", stderr="", exit_code=1)  # has changes
        return CommandResult(stdout="", stderr="", exit_code=0)

    async def run_tests(self, project_id: str, framework: str = "pytest"):
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
    assert update["task_queue"] == [{"description": "Do the trivial thing"}]


def test_node_coder_returns_reviewing_phase_when_queue_already_empty():
    state = new_project_state("proj-1")
    state["task_queue"] = []

    update = node_coder(state, router=None, executor_client=None)

    assert update == {"phase": "reviewing"}


def test_node_coder_succeeds_and_advances_to_reviewing_when_last_task_done():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == []
    assert update["current_task"] is None
    assert update["phase"] == "reviewing"


def test_node_coder_keeps_implementing_when_more_tasks_remain():
    router = FakeRouter([CODER_DONE])
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "First task"}, {"description": "Second task"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [{"description": "Second task"}]
    assert update["current_task"] is None
    assert update["phase"] == "implementing"


def test_node_coder_retries_a_failed_task_below_the_attempt_limit():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "implementing"
    assert update["current_task"]["attempts"] == 1
    assert update["current_task"]["description"] == "Do the trivial thing"


def test_node_coder_blocks_after_exhausting_the_attempt_budget():
    router = FakeRouter([_tool_call("read_file", {"path": "missing.py"})] * 10)
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]
    state["current_task"] = {
        "description": "Do the trivial thing",
        "attempts": MAX_TASK_ATTEMPTS - 1,
    }

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "blocked"
    assert update["curator_source"] == "coder"
    assert update["current_task"]["attempts"] == MAX_TASK_ATTEMPTS


def test_route_after_coder_routes_to_memory_curator_when_blocked():
    """Exhausted retries are a curation-worthy transition into blocked (Phase 14)."""
    state = new_project_state("proj-1")
    state["phase"] = "blocked"

    assert route_after_coder(state) == "memory_curator"


def test_route_after_coder_advances_to_reviewer_when_reviewing():
    state = new_project_state("proj-1")
    state["phase"] = "reviewing"

    assert route_after_coder(state) == "reviewer"


def test_route_after_coder_loops_back_when_still_implementing():
    state = new_project_state("proj-1")
    state["phase"] = "implementing"

    assert route_after_coder(state) == "coder"


def test_node_cost_check_returns_empty_update_below_threshold():
    state = new_project_state("proj-1", cost_usd_session=COST_THRESHOLD_USD_STUB - 1)

    assert cost_check(state) == {}


def test_node_cost_check_arms_gate_at_threshold():
    state = new_project_state("proj-1", cost_usd_session=COST_THRESHOLD_USD_STUB)

    update = cost_check(state)

    assert update["phase"] == "awaiting_cost_approval"
    assert update["pending_approval"] == {
        "kind": "cost_alert",
        "thread_id": "proj-1",
        "project_id": "proj-1",
        "cost_usd_session": COST_THRESHOLD_USD_STUB,
        "threshold_usd": COST_THRESHOLD_USD_STUB,
        "summary": (
            f"Crossed ${COST_THRESHOLD_USD_STUB:.2f} of "
            f"${COST_THRESHOLD_USD_STUB:.2f} session cost limit -- continue?"
        ),
    }


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

    update = node_reviewer(state, router, _RecordingExecutorClient())

    assert update["phase"] == "testing"
    assert update["cost_usd_session"] > 0


def test_node_reviewer_requests_changes_and_routes_back_to_coder():
    router = FakeRouter([_CHANGES_REQUESTED_REVIEW])
    state = new_project_state("proj-1")

    update = node_reviewer(state, router, _RecordingExecutorClient())

    assert update["phase"] == "implementing"
    assert len(update["messages"]) == 1
    assert "Reviewer feedback:" in update["messages"][0].content
    assert "models.py:15 [critical] hardcoded password in source code" in update["messages"][0].content


def test_route_after_reviewer_advances_to_frontend_review_when_approved():
    state = new_project_state("proj-1")
    state["phase"] = "testing"

    assert route_after_reviewer(state) == "frontend_review"


def test_route_after_reviewer_loops_back_to_coder_on_changes_requested():
    state = new_project_state("proj-1")
    state["phase"] = "implementing"

    assert route_after_reviewer(state) == "coder"


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
