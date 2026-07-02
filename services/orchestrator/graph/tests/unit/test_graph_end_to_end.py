"""End-to-end tests for the compiled project subgraph.

Uses a real SqliteSaver against a tmp_path file rather than an in-memory
saver specifically so `test_pending_interrupt_survives_a_simulated_restart`
can prove persistence: an ephemeral in-memory checkpointer would trivially
"survive" a restart within the same test process, proving nothing.
"""

from langgraph.types import Command

from services.orchestrator.graph.build import build_project_graph
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.nodes import COST_THRESHOLD_USD_STUB
from services.orchestrator.graph.state import new_project_state


def _config(project_id: str) -> dict:
    return {"configurable": {"thread_id": project_id}}


def test_project_below_cost_threshold_skips_cost_gate_and_pauses_at_merge_gate(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(checkpointer)
    config = _config("proj-under-budget")

    graph.invoke(new_project_state("proj-under-budget"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_merge_approval"
    assert snapshot.values["pending_approval"] == {
        "kind": "merge_approval",
        "project_id": "proj-under-budget",
    }
    assert snapshot.next == ("merge_wait",)
    assert any(task.interrupts for task in snapshot.tasks)


def test_cost_interrupt_fires_at_threshold(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(checkpointer)
    config = _config("proj-over-budget")

    graph.invoke(
        new_project_state("proj-over-budget", cost_usd_session=COST_THRESHOLD_USD_STUB),
        config=config,
    )

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_cost_approval"
    assert snapshot.values["pending_approval"]["kind"] == "cost_alert"
    assert snapshot.next == ("cost_wait",)
    assert any(task.interrupts for task in snapshot.tasks)


def test_resume_continue_after_cost_interrupt_reaches_merge_gate(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(checkpointer)
    config = _config("proj-resume-continue")
    graph.invoke(
        new_project_state("proj-resume-continue", cost_usd_session=COST_THRESHOLD_USD_STUB),
        config=config,
    )

    graph.invoke(Command(resume="continue"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "awaiting_merge_approval"
    assert snapshot.next == ("merge_wait",)


def test_resume_pause_after_cost_interrupt_blocks_and_halts_the_run(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(checkpointer)
    config = _config("proj-resume-pause")
    graph.invoke(
        new_project_state("proj-resume-pause", cost_usd_session=COST_THRESHOLD_USD_STUB),
        config=config,
    )

    graph.invoke(Command(resume="pause"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "blocked"
    assert snapshot.next == ()


def test_resume_reject_at_merge_gate_blocks_and_halts_the_run(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(checkpointer)
    config = _config("proj-reject-merge")
    graph.invoke(new_project_state("proj-reject-merge"), config=config)

    graph.invoke(Command(resume="reject"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "blocked"
    assert snapshot.next == ()


def test_resume_approve_at_merge_gate_reaches_done(tmp_path):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(checkpointer)
    config = _config("proj-approve-merge")
    graph.invoke(new_project_state("proj-approve-merge"), config=config)

    graph.invoke(Command(resume="approve"), config=config)

    snapshot = graph.get_state(config)
    assert snapshot.values["phase"] == "done"
    assert snapshot.next == ()


def test_pending_interrupt_survives_a_simulated_restart(tmp_path):
    """Proves checkpointer persistence, not just in-memory graph state.

    Builds the graph, trips the cost gate, then closes the sqlite
    connection and opens a brand-new one against the same file (simulating
    `./foundry down` followed by `./foundry up`) before resuming -- exactly
    the scenario Phase 5's Definition of Done requires.
    """
    db_path = tmp_path / "foundry_memory.db"
    project_id = "proj-restart"
    config = _config(project_id)

    checkpointer_before_restart = build_sqlite_checkpointer(db_path)
    graph_before_restart = build_project_graph(checkpointer_before_restart)
    graph_before_restart.invoke(
        new_project_state(project_id, cost_usd_session=COST_THRESHOLD_USD_STUB),
        config=config,
    )
    snapshot_before_restart = graph_before_restart.get_state(config)
    assert snapshot_before_restart.values["phase"] == "awaiting_cost_approval"
    checkpointer_before_restart.conn.close()

    checkpointer_after_restart = build_sqlite_checkpointer(db_path)
    graph_after_restart = build_project_graph(checkpointer_after_restart)

    snapshot_after_restart = graph_after_restart.get_state(config)
    assert snapshot_after_restart.values["phase"] == "awaiting_cost_approval"
    assert snapshot_after_restart.values["pending_approval"]["kind"] == "cost_alert"
    assert snapshot_after_restart.next == ("cost_wait",)
    assert any(task.interrupts for task in snapshot_after_restart.tasks)

    graph_after_restart.invoke(Command(resume="continue"), config=config)
    graph_after_restart.invoke(Command(resume="approve"), config=config)

    final_snapshot = graph_after_restart.get_state(config)
    assert final_snapshot.values["phase"] == "done"
    assert final_snapshot.next == ()
