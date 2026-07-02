"""Unit tests for node bodies and gate/routing functions.

`node_planner_wait`, `node_cost_wait`, `node_merge_wait`, and `node_plan_wait`
call `interrupt()`, which relies on LangGraph's runtime context and can't be
exercised by calling the bare function directly -- they're covered end to
end in `test_graph_end_to_end.py` instead.
"""

from langgraph.graph import END

from services.orchestrator.graph.nodes import (
    COST_THRESHOLD_USD_STUB,
    node_done,
    node_implementing,
    node_intake,
    node_planner_ask,
    node_planner_critique,
    node_planner_draft,
    node_testing,
    route_after_cost_check,
    route_after_plan_wait,
    route_after_planner_ask,
)
from services.orchestrator.graph.nodes import node_cost_check as cost_check
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import (
    EMPTY_DRAFT_RESPONSE,
    NO_FINDINGS_CRITIQUE_RESPONSE,
    READY_RESPONSE,
    FakeRouter,
)


def test_node_intake_advances_to_planning():
    state = new_project_state("proj-1")

    assert node_intake(state) == {"phase": "planning"}


def test_node_implementing_advances_to_testing():
    state = new_project_state("proj-1")

    assert node_implementing(state) == {"phase": "testing"}


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


def test_route_after_plan_wait_halts_when_blocked():
    state = new_project_state("proj-1")
    state["phase"] = "blocked"

    assert route_after_plan_wait(state) == END


def test_route_after_plan_wait_advances_when_implementing():
    state = new_project_state("proj-1")
    state["phase"] = "implementing"

    assert route_after_plan_wait(state) == "implementing"


def test_node_done_returns_empty_update():
    state = new_project_state("proj-1")
    state["phase"] = "done"

    assert node_done(state) == {}
