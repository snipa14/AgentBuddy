"""Unit tests for node bodies and gate/routing functions.

`node_planner_wait`, `node_cost_wait`, `node_merge_wait`, and `node_plan_wait`
call `interrupt()`, which relies on LangGraph's runtime context and can't be
exercised by calling the bare function directly -- they're covered end to
end in `test_graph_end_to_end.py` instead. `node_coder` never calls
`interrupt()` at all, so it's fully testable directly like any other node.
"""

from langgraph.graph import END

from services.orchestrator.executor_client import ExecutorClientError
from services.orchestrator.graph.nodes import (
    COST_THRESHOLD_USD_STUB,
    MAX_TASK_ATTEMPTS,
    node_coder,
    node_done,
    node_intake,
    node_planner_ask,
    node_planner_critique,
    node_planner_draft,
    node_task_decomposition,
    node_testing,
    route_after_coder,
    route_after_cost_check,
    route_after_plan_wait,
    route_after_planner_ask,
)
from services.orchestrator.graph.nodes import node_cost_check as cost_check
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import (
    CODER_DONE_RESPONSE,
    EMPTY_DRAFT_RESPONSE,
    NO_FINDINGS_CRITIQUE_RESPONSE,
    ONE_TASK_RESPONSE,
    READY_RESPONSE,
    FakeRouter,
)


class _RecordingExecutorClient:
    """Minimal fake matching ExecutorClient's async surface, recording calls."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.calls: list[tuple] = []

    async def read_file(self, project_id, path):
        self.calls.append(("read_file", project_id, path))
        if path not in self.files:
            raise ExecutorClientError(f"404: '{path}' not found")
        return self.files[path]

    async def write_file(self, project_id, path, content):
        self.calls.append(("write_file", project_id, path, content))
        self.files[path] = content
        return True


def test_node_intake_advances_to_planning():
    state = new_project_state("proj-1")

    assert node_intake(state) == {"phase": "planning"}


def test_node_planner_ask_arms_the_question_gate_when_more_info_needed():
    questions_response = "## Status\nquestions\n\n## Questions\n- What language?\n"
    router = FakeRouter([questions_response])
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


def test_node_planner_ask_advances_to_roasting_when_ready():
    router = FakeRouter([READY_RESPONSE])
    state = new_project_state("proj-1", initial_message="build a thing")

    update = node_planner_ask(state, router)

    assert update == {"phase": "roasting", "pending_approval": None}


def test_route_after_planner_ask_waits_when_questioning():
    state = new_project_state("proj-1")
    state["phase"] = "planning"

    assert route_after_planner_ask(state) == "planner_wait"


def test_route_after_planner_ask_drafts_when_ready():
    state = new_project_state("proj-1")
    state["phase"] = "roasting"

    assert route_after_planner_ask(state) == "planner_draft"


def test_node_planner_draft_produces_a_plan():
    router = FakeRouter([EMPTY_DRAFT_RESPONSE])
    state = new_project_state("proj-1", initial_message="build a thing")

    update = node_planner_draft(state, router)

    assert update["phase"] == "roasting"
    assert update["plan"]["summary"] == "A minimal test project."


def test_node_planner_critique_arms_the_plan_approval_gate():
    router = FakeRouter([NO_FINDINGS_CRITIQUE_RESPONSE])
    state = new_project_state("proj-1")
    state["plan"] = {"summary": "A minimal test project.", "risks": [], "assumptions": []}

    update = node_planner_critique(state, router)

    assert update["phase"] == "awaiting_plan_approval"
    assert update["pending_approval"]["kind"] == "plan_approval"
    assert update["pending_approval"]["critique_findings"] == []
    assert update["plan"]["summary"] == "A minimal test project."


def test_route_after_plan_wait_halts_when_blocked():
    state = new_project_state("proj-1")
    state["phase"] = "blocked"

    assert route_after_plan_wait(state) == END


def test_route_after_plan_wait_advances_to_task_decomposition():
    state = new_project_state("proj-1")
    state["phase"] = "implementing"

    assert route_after_plan_wait(state) == "task_decomposition"


def test_node_task_decomposition_populates_task_queue():
    router = FakeRouter([ONE_TASK_RESPONSE])
    state = new_project_state("proj-1")
    state["plan"] = {"summary": "A minimal test project."}

    update = node_task_decomposition(state, router)

    assert update["phase"] == "implementing"
    assert update["task_queue"] == [{"description": "Do the trivial thing"}]


def test_node_coder_returns_testing_phase_when_queue_already_empty():
    state = new_project_state("proj-1")
    state["task_queue"] = []

    update = node_coder(state, router=None, executor_client=None)

    assert update == {"phase": "testing"}


def test_node_coder_succeeds_and_advances_when_last_task_done():
    router = FakeRouter([CODER_DONE_RESPONSE])
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update == {"task_queue": [], "current_task": None, "phase": "testing"}


def test_node_coder_keeps_implementing_when_more_tasks_remain():
    router = FakeRouter([CODER_DONE_RESPONSE])
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "First task"}, {"description": "Second task"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["task_queue"] == [{"description": "Second task"}]
    assert update["current_task"] is None
    assert update["phase"] == "implementing"


def test_node_coder_retries_a_failed_task_below_the_attempt_limit():
    read_missing_file_response = "## Action\nread_file\n\n## Path\nmissing.py\n"
    router = FakeRouter([read_missing_file_response] * 10)
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "implementing"
    assert update["current_task"]["attempts"] == 1
    assert update["current_task"]["description"] == "Do the trivial thing"


def test_node_coder_blocks_after_exhausting_the_attempt_budget():
    read_missing_file_response = "## Action\nread_file\n\n## Path\nmissing.py\n"
    router = FakeRouter([read_missing_file_response] * 100)
    state = new_project_state("proj-1")
    state["task_queue"] = [{"description": "Do the trivial thing"}]
    state["current_task"] = {
        "description": "Do the trivial thing",
        "attempts": MAX_TASK_ATTEMPTS - 1,
    }

    update = node_coder(state, router, executor_client=_RecordingExecutorClient())

    assert update["phase"] == "blocked"
    assert update["current_task"]["attempts"] == MAX_TASK_ATTEMPTS


def test_route_after_coder_halts_when_blocked():
    state = new_project_state("proj-1")
    state["phase"] = "blocked"

    assert route_after_coder(state) == END


def test_route_after_coder_advances_to_cost_check_when_testing():
    state = new_project_state("proj-1")
    state["phase"] = "testing"

    assert route_after_coder(state) == "cost_check"


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


def test_node_testing_arms_merge_gate():
    state = new_project_state("proj-1")

    update = node_testing(state)

    assert update["phase"] == "awaiting_merge_approval"
    assert update["pending_approval"] == {
        "kind": "merge_approval",
        "thread_id": "proj-1",
        "project_id": "proj-1",
        "summary": "Project proj-1: tests passed -- merge to main?",
    }


def test_node_done_returns_empty_update():
    state = new_project_state("proj-1")
    state["phase"] = "done"

    assert node_done(state) == {}
