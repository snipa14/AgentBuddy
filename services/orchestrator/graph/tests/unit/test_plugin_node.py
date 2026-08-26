"""Unit tests for the generic plugin graph node."""

import asyncio

import pytest

from services.orchestrator.agents.tests.unit.router_fakes import FakeRouter, structured
from services.orchestrator.graph.plugin_node import (
    PluginFileReadError,
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
        self.calls: list[tuple] = []

    async def read_file(self, project_id: str, path: str) -> str:
        self.calls.append(("read_file", project_id, path))
        return self.files[path]

    async def list_files(self, project_id: str, path: str = "."):
        self.calls.append(("list_files", project_id, path))
        from services.orchestrator.executor_client import FileEntry

        return [FileEntry(name=name, is_dir=False) for name in self.files]


def _gating_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": "security-audit-reviewer",
            "role": "security_audit_reviewer",
            "slot": "after_reviewer",
            "mode": "gating",
            "requires": ["tool_calling"],
        }
    )


def _advisory_manifest() -> PluginManifest:
    return PluginManifest.model_validate(
        {
            "name": "style-advisor",
            "role": "style_advisor",
            "slot": "after_coder",
            "mode": "advisory",
            "requires": [],
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
        run_plugin_review(
            router, executor, "proj-1", "security_audit_reviewer", "Skill body.", ["a.py"]
        )
    )

    assert result.verdict == "approve"
    assert message.usage_metadata is not None
    assert "Skill body." in router.model.requests[0][0].content


def test_run_plugin_review_lists_whole_workspace_when_changed_files_empty():
    router = FakeRouter([structured(APPROVE)])
    executor = RecordingExecutorClient(files={"a.py": "print(1)\n", "b.py": "print(2)\n"})

    result, message = asyncio.run(
        run_plugin_review(router, executor, "proj-1", "security_audit_reviewer", "Skill body.", [])
    )

    assert result.verdict == "approve"
    assert ("list_files", "proj-1", ".") in executor.calls
    user_message = router.model.requests[0][1]
    assert "a.py" in user_message.content
    assert "b.py" in user_message.content


def test_run_plugin_review_skips_unreadable_files_gracefully_in_fallback():
    router = FakeRouter([structured(APPROVE)])

    class FailingExecutorClient:
        async def list_files(self, project_id, path="."):
            from services.orchestrator.executor_client import FileEntry

            return [FileEntry(name="a.py", is_dir=False), FileEntry(name="bad.py", is_dir=False)]

        async def read_file(self, project_id, path):
            if path == "bad.py":
                raise FileNotFoundError("nope")
            return "print(1)\n"

    result, _ = asyncio.run(
        run_plugin_review(
            router, FailingExecutorClient(), "proj-1", "security_audit_reviewer", "Skill body.", []
        )
    )

    assert result.verdict == "approve"
    user_message = router.model.requests[0][1]
    assert "a.py" in user_message.content
    assert "bad.py" not in user_message.content


def test_run_plugin_review_raises_when_all_changed_files_fail_to_read():
    router = FakeRouter([])

    class AlwaysFailingExecutorClient:
        async def read_file(self, project_id, path):
            raise TimeoutError("executor unreachable")

    with pytest.raises(PluginFileReadError):
        asyncio.run(
            run_plugin_review(
                router,
                AlwaysFailingExecutorClient(),
                "proj-1",
                "security_audit_reviewer",
                "Skill body.",
                ["a.py", "b.py"],
            )
        )

    assert router.model.requests == []


def test_run_plugin_review_raises_when_list_files_fails_in_fallback():
    router = FakeRouter([])

    class ListFailingExecutorClient:
        async def list_files(self, project_id, path="."):
            raise ConnectionError("executor unreachable")

        async def read_file(self, project_id, path):
            raise AssertionError("read_file should not be called when list_files fails")

    with pytest.raises(PluginFileReadError):
        asyncio.run(
            run_plugin_review(
                router,
                ListFailingExecutorClient(),
                "proj-1",
                "security_audit_reviewer",
                "Skill body.",
                [],
            )
        )

    assert router.model.requests == []


def test_run_plugin_review_raises_when_all_listed_files_fail_to_read_in_fallback():
    router = FakeRouter([])

    class AllReadsFailExecutorClient:
        async def list_files(self, project_id, path="."):
            from services.orchestrator.executor_client import FileEntry

            return [FileEntry(name="a.py", is_dir=False), FileEntry(name="b.py", is_dir=False)]

        async def read_file(self, project_id, path):
            raise TimeoutError("executor unreachable")

    with pytest.raises(PluginFileReadError):
        asyncio.run(
            run_plugin_review(
                router,
                AllReadsFailExecutorClient(),
                "proj-1",
                "security_audit_reviewer",
                "Skill body.",
                [],
            )
        )

    assert router.model.requests == []


def test_run_plugin_review_proceeds_when_fallback_lists_zero_files():
    router = FakeRouter([structured(APPROVE)])

    class EmptyProjectExecutorClient:
        async def list_files(self, project_id, path="."):
            return []

        async def read_file(self, project_id, path):
            raise AssertionError("read_file should not be called for an empty listing")

    result, _ = asyncio.run(
        run_plugin_review(
            router,
            EmptyProjectExecutorClient(),
            "proj-1",
            "security_audit_reviewer",
            "Skill body.",
            [],
        )
    )

    assert result.verdict == "approve"


class _AlwaysFailingExecutorClient:
    async def read_file(self, project_id, path):
        raise TimeoutError("executor unreachable")


def test_node_plugin_gating_file_read_failure_reopens_task_instead_of_approving():
    router = FakeRouter([])
    state = _state(
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}],
        changed_files=["a.py"],
    )

    update = node_plugin(
        state,
        router=router,
        executor_client=_AlwaysFailingExecutorClient(),
        manifest=_gating_manifest(),
        skill_body="Skill body.",
        next_node="frontend_review",
    )

    assert router.model.requests == []
    assert update["phase"] == "implementing"
    assert update["task_queue"][0]["attempts"] == 1
    assert update["pending_approval"] is None
    assert route_after_plugin({**state, **update}, next_node="frontend_review") == "coder"


def test_node_plugin_gating_file_read_failure_blocks_with_no_reopenable_task():
    router = FakeRouter([])
    state = _state(task_queue=[], changed_files=["a.py"])

    update = node_plugin(
        state,
        router=router,
        executor_client=_AlwaysFailingExecutorClient(),
        manifest=_gating_manifest(),
        skill_body="Skill body.",
        next_node="frontend_review",
    )

    assert router.model.requests == []
    assert update["phase"] == "blocked"
    assert update["curator_source"] == _gating_manifest().name
    assert route_after_plugin({**state, **update}, next_node="frontend_review") == "memory_curator"


def test_node_plugin_advisory_file_read_failure_continues_to_next_node():
    router = FakeRouter([])
    state = _state(
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}],
        changed_files=["a.py"],
    )

    update = node_plugin(
        state,
        router=router,
        executor_client=_AlwaysFailingExecutorClient(),
        manifest=_advisory_manifest(),
        skill_body="Skill body.",
        next_node="reviewer",
    )

    assert router.model.requests == []
    assert update == {}
    assert route_after_plugin({**state, **update}, next_node="reviewer") == "reviewer"


def test_node_plugin_gating_changes_requested_reopens_task_and_routes_to_coder():
    router = FakeRouter([structured(CHANGES_REQUESTED)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _state(
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}],
        changed_files=["a.py"],
    )

    update = node_plugin(
        state,
        router=router,
        executor_client=executor,
        manifest=_gating_manifest(),
        skill_body="Skill body.",
        next_node="frontend_review",
    )

    assert update["phase"] == "implementing"
    assert update["task_queue"][0]["attempts"] == 1
    assert route_after_plugin({**state, **update}, next_node="frontend_review") == "coder"


def test_node_plugin_gating_changes_requested_clears_stale_pending_approval():
    router = FakeRouter([structured(CHANGES_REQUESTED)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _state(
        task_queue=[{"description": "t1", "status": "done", "attempts": 0}],
        changed_files=["a.py"],
    )
    state["pending_approval"] = {
        "kind": "merge_approval",
        "thread_id": "proj-1",
        "project_id": "proj-1",
        "summary": "Project proj-1: tests passed -- merge to main?",
    }

    update = node_plugin(
        state,
        router=router,
        executor_client=executor,
        manifest=_gating_manifest(),
        skill_body="Skill body.",
        next_node="frontend_review",
    )

    assert update["pending_approval"] is None


def test_node_plugin_gating_approve_continues_to_next_node():
    router = FakeRouter([structured(APPROVE)])
    executor = RecordingExecutorClient(files={"a.py": "x"})
    state = _state(task_queue=[], changed_files=["a.py"])

    update = node_plugin(
        state,
        router=router,
        executor_client=executor,
        manifest=_gating_manifest(),
        skill_body="Skill body.",
        next_node="frontend_review",
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
        state,
        router=router,
        executor_client=executor,
        manifest=_advisory_manifest(),
        skill_body="Skill body.",
        next_node="reviewer",
    )

    assert "task_queue" not in update
    assert route_after_plugin({**state, **update}, next_node="reviewer") == "reviewer"
