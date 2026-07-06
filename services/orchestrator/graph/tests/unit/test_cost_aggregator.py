"""Unit tests for other_projects_cost_usd_weekly (TODO.md Phase 18).

Uses a lightweight fake checkpointer (duck-typed against
langgraph.checkpoint.base.BaseCheckpointSaver's .list()/.get_tuple()
surface) rather than a real SqliteSaver, so this suite has no langgraph
dependency at all and runs in any environment.
"""

from dataclasses import dataclass, field

from services.orchestrator.graph.cost_aggregator import other_projects_cost_usd_weekly


@dataclass
class _FakeCheckpointTuple:
    thread_id: str
    cost_usd_weekly: float

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}}

    @property
    def checkpoint(self) -> dict:
        return {"channel_values": {"cost_usd_weekly": self.cost_usd_weekly}}


@dataclass
class _FakeCheckpointer:
    """Simulates multiple historical checkpoint versions per thread_id --
    .list() returns every version ever written (oldest first, matching
    SqliteSaver's real behavior), .get_tuple() returns only the latest."""

    versions_by_thread: dict[str, list[float]] = field(default_factory=dict)

    def list(self, config):
        for thread_id, versions in self.versions_by_thread.items():
            for cost in versions:
                yield _FakeCheckpointTuple(thread_id, cost)

    def get_tuple(self, config):
        thread_id = config["configurable"]["thread_id"]
        versions = self.versions_by_thread.get(thread_id)
        if not versions:
            return None
        return _FakeCheckpointTuple(thread_id, versions[-1])


def test_other_projects_cost_usd_weekly_sums_latest_checkpoint_per_thread():
    checkpointer = _FakeCheckpointer(
        {
            "proj-a": [5.0, 5.0, 12.0],  # three historical versions -- only 12.0 should count
            "proj-b": [3.0],
        }
    )

    total = other_projects_cost_usd_weekly(checkpointer, exclude_project_id="proj-c")

    assert total == 15.0  # 12.0 (proj-a's latest) + 3.0 (proj-b's latest), NOT 5+5+12+3


def test_other_projects_cost_usd_weekly_excludes_the_given_project():
    checkpointer = _FakeCheckpointer({"proj-a": [10.0], "proj-b": [3.0]})

    total = other_projects_cost_usd_weekly(checkpointer, exclude_project_id="proj-a")

    assert total == 3.0


def test_other_projects_cost_usd_weekly_returns_zero_with_no_other_projects():
    checkpointer = _FakeCheckpointer({"proj-a": [10.0]})

    total = other_projects_cost_usd_weekly(checkpointer, exclude_project_id="proj-a")

    assert total == 0.0
