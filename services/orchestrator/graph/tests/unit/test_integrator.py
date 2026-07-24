"""Phase 13 — Git / GitHub Integrator tests.

The "blocked without approval" test is the one that matters most here — write it
first, not last (TODO.md Phase 13).
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

from langgraph.types import Command

from services.orchestrator.executor_client import CommandResult, ExecutorClientError
from services.orchestrator.graph.build import build_project_graph
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.graph.nodes import node_done, route_after_merge_wait
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.graph.tests.unit.planner_fakes import fast_track_router
from services.orchestrator.integrator_client import IntegratorClient
from services.orchestrator.stacks import StacksConfig


# -- Recording executor client (real ExecutorClient.run_command contract: an
#    argv list in, a CommandResult out -- see integrator_client.py's module
#    docstring for why this matters) ----------------------------------------

class _RecordingExecutorClient:
    """Minimal fake matching ExecutorClient's async surface, recording calls."""

    def __init__(self, files: dict[str, str] | None = None, diff_has_changes: bool = True):
        self.files = dict(files or {})
        self.calls: list[tuple] = []
        self._diff_has_changes = diff_has_changes

    async def read_file(self, project_id, path, stack: str = "python"):
        self.calls.append(("read_file", project_id, path))
        if path not in self.files:
            raise ExecutorClientError(f"404: '{path}' not found")
        return self.files[path]

    async def write_file(self, project_id, path, content, stack: str = "python"):
        self.calls.append(("write_file", project_id, path, content))
        self.files[path] = content
        return True

    async def run_command(
        self, project_id: str, command: list[str], timeout=None, stack: str = "python"
    ) -> CommandResult:
        self.calls.append(("run_command", project_id, tuple(command)))
        if tuple(command[:3]) == ("git", "diff", "--cached"):
            return CommandResult(stdout="", stderr="", exit_code=1 if self._diff_has_changes else 0)
        return CommandResult(stdout="", stderr="", exit_code=0)

    async def run_tests(self, project_id: str, framework: str = "pytest", stack: str = "python"):
        self.calls.append(("run_tests", project_id, framework))
        return MagicMock(passed=3, failed=0, coverage_pct=100.0)


# -- The critical test: blocked without approval --------------------------------


class TestBlockedWithoutApproval:
    """Prove that no merge/PR/push call is ever made unless the user approves."""

    def test_merge_wait_interrupt_blocks_all_git_operations(
        self, tmp_path, memory_conn, projects_root
    ):
        """The most important test: without resuming at merge_wait, no git command runs.

        Drives the real compiled graph through the fast-track flow up to
        merge_wait's interrupt() and stops -- proving the interrupt gate
        actually works (LangGraph never proceeds past an interrupt() until
        the graph is explicitly resumed with a Command), not just that a
        hand-crafted state dict happens to say "blocked".
        """
        executor = _RecordingExecutorClient()
        checkpointer = build_sqlite_checkpointer(tmp_path / "foundry_memory.db")
        graph = build_project_graph(
            checkpointer,
            fast_track_router(),
            executor,
            memory_conn,
            projects_root,
            StacksConfig(stacks={}),
        )
        config = {"configurable": {"thread_id": "proj-blocked"}}

        graph.invoke(new_project_state("proj-blocked"), config=config)
        graph.invoke(Command(resume="approve"), config=config)

        snapshot = graph.get_state(config)
        assert snapshot.values["phase"] == "awaiting_merge_approval"
        assert snapshot.next == ("merge_wait",)

        git_calls = [c for c in executor.calls if c[0] == "run_command"]
        assert git_calls == [], (
            f"No git operations should execute without merge approval, but found: {git_calls}"
        )

    def test_node_done_is_a_local_commit_without_git_remote_url(self):
        """node_done never pushes/opens a PR when git_remote_url is missing --
        it still commits locally (Phase 13's "commits always happen
        autonomously" goal), but no run_command touches origin/push."""
        executor = _RecordingExecutorClient()
        state = new_project_state("proj-1")
        state["phase"] = "done"

        update = node_done(state, executor)

        assert update == {"phase": "done"}
        push_calls = [c for c in executor.calls if c[0] == "run_command" and "push" in c[2]]
        assert push_calls == []


# -- Auto mode tests ------------------------------------------------------------


class TestAutoMode:
    """pr_mode=auto: commit, push, and open a PR via GitHub API."""

    def test_node_done_auto_creates_pr(self, monkeypatch):
        executor = _RecordingExecutorClient()
        state = new_project_state("proj-auto", initial_message="test")
        state["phase"] = "done"
        state["git_branch"] = "feature/proj-auto"
        state["git_remote_url"] = "git@github.com:matete/foundry.git"
        state["pr_mode"] = "auto"

        with patch("services.orchestrator.integrator_client.Github") as mock_gh_cls:
            mock_repo = MagicMock()
            mock_pr = MagicMock()
            mock_pr.html_url = "https://github.com/matete/foundry/pull/42"
            mock_repo.create_pull.return_value = mock_pr
            mock_gh_cls.return_value.get_repo.return_value = mock_repo

            monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake123")

            update = node_done(state, executor)

        assert update == {"phase": "done"}
        cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
        assert any(c[2][:2] == ("git", "add") for c in cmd_calls)
        assert any(c[2][:2] == ("git", "commit") for c in cmd_calls)
        assert any(c[2][:2] == ("git", "push") for c in cmd_calls)
        mock_repo.create_pull.assert_called_once()
        call_kwargs = mock_repo.create_pull.call_args.kwargs
        assert call_kwargs["head"] == "feature/proj-auto"
        assert call_kwargs["base"] == "main"

    def test_node_done_auto_pr_creation_failure_still_completes(self, monkeypatch):
        """PR creation failure should not block the done phase."""
        executor = _RecordingExecutorClient()
        state = new_project_state("proj-auto-fail", initial_message="test")
        state["phase"] = "done"
        state["git_branch"] = "feature/proj-auto-fail"
        state["git_remote_url"] = "git@github.com:matete/foundry.git"
        state["pr_mode"] = "auto"

        with patch("services.orchestrator.integrator_client.Github") as mock_gh_cls:
            mock_repo = MagicMock()
            mock_repo.create_pull.side_effect = Exception("API rate limit exceeded")
            mock_gh_cls.return_value.get_repo.return_value = mock_repo

            monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake123")

            update = node_done(state, executor)

        assert update == {"phase": "done"}


# -- Manual mode tests ----------------------------------------------------------


class TestManualMode:
    """pr_mode=manual: commit and push the branch, report branch name."""

    def test_node_done_manual_pushes_branch(self):
        executor = _RecordingExecutorClient()
        state = new_project_state("proj-manual", initial_message="test")
        state["phase"] = "done"
        state["git_branch"] = "feature/proj-manual"
        state["git_remote_url"] = "git@github.com:matete/foundry.git"
        state["pr_mode"] = "manual"

        update = node_done(state, executor)

        assert update == {"phase": "done"}
        cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
        assert any(c[2][:2] == ("git", "add") for c in cmd_calls)
        assert any(c[2][:2] == ("git", "commit") for c in cmd_calls)
        assert any(c[2][:2] == ("git", "push") for c in cmd_calls)

    def test_node_done_manual_no_pr_call_without_github_token(self):
        """Manual mode should not attempt GitHub API calls regardless of token."""
        executor = _RecordingExecutorClient()
        state = new_project_state("proj-manual-no-token", initial_message="test")
        state["phase"] = "done"
        state["git_branch"] = "feature/proj-manual-no-token"
        state["git_remote_url"] = "git@github.com:matete/foundry.git"
        state["pr_mode"] = "manual"

        saved_token = os.environ.pop("GITHUB_TOKEN", None)
        try:
            update = node_done(state, executor)
        finally:
            if saved_token is not None:
                os.environ["GITHUB_TOKEN"] = saved_token

        assert update == {"phase": "done"}
        cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
        assert any(c[2][:2] == ("git", "push") for c in cmd_calls)


# -- IntegratorClient unit tests ------------------------------------------------


class TestIntegratorClient:
    """Unit tests for the IntegratorClient class."""

    def test_commit_and_push_no_remote_returns_branch(self):
        executor = _RecordingExecutorClient()
        client = IntegratorClient(executor)

        branch, remote = asyncio.run(
            client.commit_and_push("proj-1", branch_name="feature/test", remote_url=None)
        )

        assert branch == "feature/test"
        assert remote == ""
        cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
        assert any(c[2][:2] == ("git", "config") for c in cmd_calls)
        assert any(c[2][:2] == ("git", "add") for c in cmd_calls)
        assert not any(c[2][:2] == ("git", "push") for c in cmd_calls)

    def test_commit_and_push_with_remote(self):
        executor = _RecordingExecutorClient()
        client = IntegratorClient(executor)

        branch, remote = asyncio.run(
            client.commit_and_push(
                "proj-1",
                branch_name="feature/test",
                remote_url="git@github.com:user/repo.git",
            )
        )

        assert branch == "feature/test"
        assert remote == "git@github.com:user/repo.git"
        cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
        assert any(c[2][:2] == ("git", "push") for c in cmd_calls)

    def test_commit_and_push_skips_commit_when_nothing_changed(self):
        executor = _RecordingExecutorClient(diff_has_changes=False)
        client = IntegratorClient(executor)

        branch, remote = asyncio.run(
            client.commit_and_push("proj-1", branch_name="feature/test", remote_url=None)
        )

        assert remote == ""
        cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
        assert not any(c[2][:2] == ("git", "commit") for c in cmd_calls)

    def test_commit_and_push_never_forces_and_branches_before_committing(self):
        """The push must be a plain fast-forward (git.md forbids force
        pushes), and checkout -B must precede commit so the commit lands on
        the feature branch, never on main."""
        executor = _RecordingExecutorClient()
        client = IntegratorClient(executor)

        asyncio.run(
            client.commit_and_push(
                "proj-1",
                branch_name="feature/test",
                remote_url="git@github.com:user/repo.git",
            )
        )

        cmd_calls = [c[2] for c in executor.calls if c[0] == "run_command"]
        push_commands = [c for c in cmd_calls if c[:2] == ("git", "push")]
        assert push_commands and all("--force" not in c for c in push_commands)
        checkout_index = next(
            i for i, c in enumerate(cmd_calls) if c[:2] == ("git", "checkout")
        )
        commit_index = next(
            i for i, c in enumerate(cmd_calls) if c[:2] == ("git", "commit")
        )
        assert checkout_index < commit_index

    def test_commit_and_push_uses_argv_lists_not_shell_strings(self):
        """Every run_command call must be an argv list -- run_command execs
        directly with no shell, so a single '&&'-joined string would never work."""
        executor = _RecordingExecutorClient()
        client = IntegratorClient(executor)

        asyncio.run(
            client.commit_and_push("proj-1", branch_name="feature/test", remote_url=None)
        )

        for call in executor.calls:
            command = call[2]
            assert isinstance(command, tuple)
            assert all(isinstance(token, str) for token in command)
            assert not any("&&" in token for token in command)

    def test_commit_and_push_falls_back_to_set_url_when_remote_already_exists(self):
        class _ExistingRemoteExecutorClient(_RecordingExecutorClient):
            async def run_command(self, project_id, command, timeout=None):
                self.calls.append(("run_command", project_id, tuple(command)))
                if tuple(command[:3]) == ("git", "remote", "add"):
                    return CommandResult(stdout="", stderr="remote origin already exists", exit_code=1)
                if tuple(command[:3]) == ("git", "diff", "--cached"):
                    return CommandResult(stdout="", stderr="", exit_code=1)
                return CommandResult(stdout="", stderr="", exit_code=0)

        executor = _ExistingRemoteExecutorClient()
        client = IntegratorClient(executor)

        asyncio.run(
            client.commit_and_push(
                "proj-1", branch_name="feature/test", remote_url="git@github.com:user/repo.git"
            )
        )

        cmd_calls = [c[2] for c in executor.calls if c[0] == "run_command"]
        assert any(c[:3] == ("git", "remote", "set-url") for c in cmd_calls)

    def test_extract_repo_slug_ssh(self):
        slug = IntegratorClient._extract_repo_slug("git@github.com:matete/foundry.git")
        assert slug == "matete/foundry"

    def test_extract_repo_slug_https(self):
        slug = IntegratorClient._extract_repo_slug("https://github.com/matete/foundry.git")
        assert slug == "matete/foundry"

    def test_extract_repo_slug_empty(self):
        assert IntegratorClient._extract_repo_slug("") is None

    def test_extract_repo_slug_non_github(self):
        assert IntegratorClient._extract_repo_slug("git@gitlab.com:user/repo.git") is None


# -- Route after merge_wait tests ------------------------------------------------


class TestRouteAfterMergeWait:
    """route_after_merge_wait always routes through memory_curator now (Phase 14) --
    route_after_memory_curator is the one that actually halts with END."""

    def test_route_to_memory_curator_when_approved(self):
        state = new_project_state("proj-1")
        state["phase"] = "done"

        assert route_after_merge_wait(state) == "memory_curator"

    def test_route_to_memory_curator_when_blocked(self):
        state = new_project_state("proj-1")
        state["phase"] = "blocked"

        assert route_after_merge_wait(state) == "memory_curator"


# -- Edge cases -----------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for the integrator flow."""

    def test_node_done_configurable_mode_pushes_no_pr(self):
        """pr_mode=configurable pushes the branch but doesn't create a PR."""
        executor = _RecordingExecutorClient()
        state = new_project_state("proj-config", initial_message="test")
        state["phase"] = "done"
        state["git_branch"] = "feature/proj-config"
        state["git_remote_url"] = "git@github.com:matete/foundry.git"
        state["pr_mode"] = "configurable"

        update = node_done(state, executor)

        assert update == {"phase": "done"}
        cmd_calls = [c for c in executor.calls if c[0] == "run_command"]
        assert any(c[2][:2] == ("git", "push") for c in cmd_calls)


# -- IntegratorClient PR creation tests ------------------------------------------


class TestIntegratorClientPR:
    """Tests for IntegratorClient.create_pull_request."""

    def test_create_pr_success(self, monkeypatch):
        executor = _RecordingExecutorClient()
        client = IntegratorClient(executor)

        with patch("services.orchestrator.integrator_client.Github") as mock_gh_cls:
            mock_repo = MagicMock()
            mock_pr = MagicMock()
            mock_pr.html_url = "https://github.com/test/repo/pull/1"
            mock_repo.create_pull.return_value = mock_pr
            mock_gh_cls.return_value.get_repo.return_value = mock_repo

            monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

            success, detail = asyncio.run(
                client.create_pull_request(
                    title="Test PR",
                    body="Test body",
                    head_branch="feature/test",
                    base_branch="main",
                    remote_url="git@github.com:test/repo.git",
                )
            )

        assert success is True
        assert detail == "https://github.com/test/repo/pull/1"
        mock_repo.create_pull.assert_called_once_with(
            title="Test PR",
            body="Test body",
            head="feature/test",
            base="main",
        )

    def test_create_pr_no_token(self, monkeypatch):
        executor = _RecordingExecutorClient()
        client = IntegratorClient(executor)

        saved_token = os.environ.pop("GITHUB_TOKEN", None)

        try:
            success, detail = asyncio.run(
                client.create_pull_request(
                    title="Test PR",
                    body="Test body",
                    head_branch="feature/test",
                    remote_url="git@github.com:test/repo.git",
                )
            )
            assert success is False
            assert "GITHUB_TOKEN" in detail
        finally:
            if saved_token:
                os.environ["GITHUB_TOKEN"] = saved_token

    def test_create_pr_invalid_remote(self, monkeypatch):
        executor = _RecordingExecutorClient()
        client = IntegratorClient(executor)

        with patch("services.orchestrator.integrator_client.Github") as mock_gh_cls:
            monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

            success, detail = asyncio.run(
                client.create_pull_request(
                    title="Test PR",
                    body="Test body",
                    head_branch="feature/test",
                    remote_url="ssh://not-a-github-url",
                )
            )

        assert success is False
        mock_gh_cls.assert_not_called()
