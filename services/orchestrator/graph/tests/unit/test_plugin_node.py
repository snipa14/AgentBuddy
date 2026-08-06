"""Unit tests for the generic plugin graph node."""

import asyncio

from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter, structured
from services.orchestrator.graph.plugin_node import (
    PluginFinding,
    PluginVerdict,
    node_plugin,
    route_after_plugin,
    run_plugin_review,
)
from services.orchestrator.graph.state import ProjectState, new_project_state
from services.orchestrator.plugins.manifest import PluginManifest

APPROVE = PluginVerdict(verdict="approve", findings=[])
CHANGES_REQUESTED = PluginVerdict(
    verdict="changes-requested",
    findings=[PluginFinding(file="a.py", line=1, severity="critical", message="bad")],
)


class RecordingExecutorClient:
    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})

    async def read_file(self, project_id: str, path: str) -> str:
        return self.files[path]


def _gating_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": "security-audit-reviewer", "role": "security_audit_reviewer",
            "slot": "after_reviewer", "mode": "gating", "requires": ["tool_calling"],
        }
    )


def _advisory_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": "style-advisor", "role": "style_advisor",
            "slot": "after_coder", "mode": "advisory", "requires": [],
        }
    )


def _state(task_queue: list[dict], changed_files: list[str]) -> ProjectState:
    state = new_project_state("proj-1")
    state["phase"] = "reviewing"
    state["task_queue"] = task_queue
    state["changed_files"] = changed_files
    return state


def test_run_plugin_review_reads_changed_files_and_returns_verdict():
    router = FakeRouter([structured(APPROVE)])
    executor = RecordingExecutorClient(files={"a.py": "print(1)\n"})

    result, message = asyncio.run(
        run_plugin_review(router, executor, "proj-1", "security_audit_reviewer", "Skill body.", ["a.py"])
    )

    assert result.verdict == "approve"
    assert message.usage_metadata is not None
    assert "Skill body." in router.model.requests[0][0].content


def test_node_plugin_gating_changes_requested_reopens_task_and_routes_to_coder():
    router = FakeRouter([structured(CHANGES_REQUESTED)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _state(
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}],
        changed_files=["a.py"],
    )

    update = node_plugin(
        state, router=router, executor_client=executor,
        manifest=_gating_manifest(), skill_body="Skill body.", next_node="frontend_review",
    )

    assert update["phase"] == "implementing"
    assert update["task_queue"][0]["attempts"] == 1
    assert route_after_plugin({**state, **update}, next_node="frontend_review") == "coder"


def test_node_plugin_gating_approve_continues_to_next_node():
    router = FakeRouter([structured(APPROVE)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _state(task_queue=[], changed_files=["a.py"])

    update = node_plugin(
        state, router=router, executor_client=executor,
        manifest=_gating_manifest(), skill_body="Skill body.", next_node="frontend_review",
    )

    assert set(update) == {"cost_usd_session", "cost_usd_weekly"}
    assert update["cost_usd_session"] > state["cost_usd_session"]
    assert route_after_plugin({**state, **update}, next_node="frontend_review") == "frontend_review"


def test_node_plugin_advisory_changes_requested_never_reopens_a_task():
    router = FakeRouter([structured(CHANGES_REQUESTED)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _state(
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}],
        changed_files=["a.py"],
    )

    update = node_plugin(
        state, router=router, executor_client=executor,
        manifest=_advisory_manifest(), skill_body="Skill body.", next_node="reviewer",
    )

    assert "task_queue" not in update
    assert route_after_plugin({**state, **update}, next_node="reviewer") == "reviewer"
