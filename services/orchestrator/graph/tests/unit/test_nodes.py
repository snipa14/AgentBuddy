"""Unit tests for the stub node bodies and gate routing functions.

`node_cost_wait` and `node_merge_wait` call `interrupt()`, which relies on
LangGraph's runtime context and can't be exercised by calling the bare
function directly -- they're covered end to end in
`test_graph_end_to_end.py` instead.
"""

from services.orchestrator.graph.nodes import (
    COST_THRESHOLD_USD_STUB,
    node_done,
    node_implementing,
    node_intake,
    node_planning,
    node_testing,
    route_after_cost_check,
)
from services.orchestrator.graph.nodes import node_cost_check as cost_check
from services.orchestrator.graph.state import new_project_state


def test_node_intake_advances_to_planning():
    state = new_project_state("proj-1")

    assert node_intake(state) == {"phase": "planning"}


def test_node_planning_advances_to_implementing():
    state = new_project_state("proj-1")

    assert node_planning(state) == {"phase": "implementing"}


def test_node_implementing_advances_to_testing():
    state = new_project_state("proj-1")

    assert node_implementing(state) == {"phase": "testing"}


def test_node_cost_check_returns_empty_update_below_threshold():
    state = new_project_state("proj-1", cost_usd_session=COST_THRESHOLD_USD_STUB - 1)

    assert cost_check(state) == {}


def test_node_cost_check_arms_gate_at_threshold():
    state = new_project_state("proj-1", cost_usd_session=COST_THRESHOLD_USD_STUB)

    update = cost_check(state)

    assert update["phase"] == "awaiting_cost_approval"
    assert update["pending_approval"] == {
        "kind": "cost_alert",
        "project_id": "proj-1",
        "cost_usd_session": COST_THRESHOLD_USD_STUB,
        "threshold_usd": COST_THRESHOLD_USD_STUB,
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
    assert update["pending_approval"] == {"kind": "merge_approval", "project_id": "proj-1"}


def test_node_done_returns_empty_update():
    state = new_project_state("proj-1")
    state["phase"] = "done"

    assert node_done(state) == {}
