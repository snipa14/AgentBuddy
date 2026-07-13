"""Unit tests for the initial ProjectState factory."""

from services.orchestrator.graph.state import new_project_state


def test_new_project_state_defaults_to_intake_phase_with_no_pending_approval():
    state = new_project_state("proj-1")

    assert state["project_id"] == "proj-1"
    assert state["phase"] == "intake"
    assert state["mode"] == "greenfield"
    assert state["pr_mode"] == "manual"
    assert state["coverage_threshold"] == 80
    assert state["coverage_gate_mode"] == "hard"
    assert state["cost_usd_session"] == 0.0
    assert state["pending_approval"] is None
    assert state["messages"] == []
    assert state["task_queue"] == []


def test_new_project_state_accepts_overrides():
    state = new_project_state(
        "proj-2",
        mode="brownfield",
        coverage_threshold=90,
        pr_mode="auto",
        coverage_gate_mode="soft",
        cost_usd_session=12.5,
    )

    assert state["mode"] == "brownfield"
    assert state["coverage_threshold"] == 90
    assert state["pr_mode"] == "auto"
    assert state["coverage_gate_mode"] == "soft"
    assert state["cost_usd_session"] == 12.5


def test_new_project_state_seeds_cost_usd_weekly():
    state = new_project_state("proj-1", cost_usd_weekly=12.5)

    assert state["cost_usd_weekly"] == 12.5
