"""Unit tests for plugin wiring in build_project_graph."""

from pathlib import Path

import pytest

from services.orchestrator.executor_client import CommandResult, TestResult
from services.orchestrator.graph.build import build_project_graph
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.tests.unit.planner_fakes import FakeRouter
from services.orchestrator.plugins.discovery import LoadedPlugin, discover_plugins
from services.orchestrator.plugins.manifest import PluginManifest
from services.orchestrator.stacks import StacksConfig


FIXTURE_PLUGINS_DIR = Path(__file__).parents[3] / "plugins" / "tests" / "fixtures"


class _StubExecutorClient:
    """Never actually invoked by these tests -- build_project_graph only
    binds it into node closures via functools.partial, and these tests
    inspect the compiled graph's node names without calling .invoke()."""

    async def run_tests(
        self, project_id: str, framework: str = "pytest", stack: str = "python"
    ) -> TestResult:
        return TestResult(passed=1, failed=0, coverage_pct=100.0, report="1 passed")

    async def run_command(self, project_id: str, command, timeout=None, stack: str = "python"):
        return CommandResult(stdout="", stderr="", exit_code=0)


def _build_graph(tmp_path, memory_conn, projects_root, plugins):
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    return build_project_graph(
        checkpointer, FakeRouter([]), _StubExecutorClient(), memory_conn, projects_root,
        StacksConfig(stacks={}), plugins=plugins,
    )


def test_build_project_graph_with_no_plugins_wires_exactly_as_before(
    tmp_path, memory_conn, projects_root
):
    graph = _build_graph(tmp_path, memory_conn, projects_root, plugins=[])

    node_names = set(graph.get_graph().nodes)
    assert "reviewer" in node_names
    assert not any(name.startswith("plugin_") for name in node_names)


def test_build_project_graph_wires_an_after_reviewer_plugin_between_reviewer_and_frontend_review(
    tmp_path, memory_conn, projects_root
):
    manifest = PluginManifest.model_validate(
        {
            "name": "security-audit-reviewer", "role": "security_audit_reviewer",
            "slot": "after_reviewer", "mode": "gating", "requires": ["tool_calling"],
        }
    )
    plugin = LoadedPlugin(manifest=manifest, skill_body="Audit for security issues.")

    graph = _build_graph(tmp_path, memory_conn, projects_root, plugins=[plugin])

    node_names = set(graph.get_graph().nodes)
    assert "plugin_security_audit_reviewer" in node_names


def test_build_project_graph_with_no_plugins_omits_default_argument(
    tmp_path, memory_conn, projects_root
):
    """build_project_graph(..., stacks_config) with plugins entirely omitted
    (the ~18 pre-existing call sites across this test package) must keep
    compiling and wiring identically to an explicit plugins=[] -- this is
    the load-bearing guarantee this whole task rests on."""
    checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
    graph = build_project_graph(
        checkpointer, FakeRouter([]), _StubExecutorClient(), memory_conn, projects_root,
        StacksConfig(stacks={}),
    )

    node_names = set(graph.get_graph().nodes)
    assert "reviewer" in node_names
    assert not any(name.startswith("plugin_") for name in node_names)


def test_build_project_graph_wires_an_after_coder_plugin_before_static_checks_reviewer_edge(
    tmp_path, memory_conn, projects_root
):
    manifest = PluginManifest.model_validate(
        {
            "name": "style-advisor", "role": "style_advisor",
            "slot": "after_coder", "mode": "advisory", "requires": [],
        }
    )
    plugin = LoadedPlugin(manifest=manifest, skill_body="Advise on style.")

    graph = _build_graph(tmp_path, memory_conn, projects_root, plugins=[plugin])

    node_names = set(graph.get_graph().nodes)
    assert "plugin_style_advisor" in node_names
    assert "reviewer" in node_names


def test_build_project_graph_wires_an_after_tester_plugin_before_memory_curator(
    tmp_path, memory_conn, projects_root
):
    manifest = PluginManifest.model_validate(
        {
            "name": "coverage-auditor", "role": "coverage_auditor",
            "slot": "after_tester", "mode": "advisory", "requires": [],
        }
    )
    plugin = LoadedPlugin(manifest=manifest, skill_body="Audit coverage.")

    graph = _build_graph(tmp_path, memory_conn, projects_root, plugins=[plugin])

    node_names = set(graph.get_graph().nodes)
    assert "plugin_coverage_auditor" in node_names
    assert "memory_curator" in node_names


def test_build_project_graph_chains_two_plugins_in_the_same_slot(
    tmp_path, memory_conn, projects_root
):
    first = PluginManifest.model_validate(
        {
            "name": "first-reviewer", "role": "first_reviewer",
            "slot": "after_reviewer", "mode": "advisory", "requires": [],
        }
    )
    second = PluginManifest.model_validate(
        {
            "name": "second-reviewer", "role": "second_reviewer",
            "slot": "after_reviewer", "mode": "advisory", "requires": [],
        }
    )
    plugins = [
        LoadedPlugin(manifest=first, skill_body="First."),
        LoadedPlugin(manifest=second, skill_body="Second."),
    ]

    graph = _build_graph(tmp_path, memory_conn, projects_root, plugins=plugins)

    node_names = set(graph.get_graph().nodes)
    assert "plugin_first_reviewer" in node_names
    assert "plugin_second_reviewer" in node_names


def test_discovered_fixture_plugin_wires_into_a_real_graph_build(
    tmp_path, memory_conn, projects_root
):
    pytest.importorskip("yaml")

    plugins = discover_plugins(FIXTURE_PLUGINS_DIR, known_roles=frozenset({"reviewer", "coder"}))
    assert [p.manifest.name for p in plugins] == ["example-plugin"]

    graph = _build_graph(tmp_path, memory_conn, projects_root, plugins=plugins)

    assert "plugin_example_reviewer" in set(graph.get_graph().nodes)
