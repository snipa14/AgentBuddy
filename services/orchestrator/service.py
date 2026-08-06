"""Global routing layer (ARCHITECTURE.md §4.2's "global graph" responsibilities).

Routing-by-thread-id, restart-safe interrupt lookup, and cross-project
listing don't need branching logic of their own, so Phase 5 implements them
as a plain Python service over the compiled per-project subgraph rather than
a second nested StateGraph. The persistent project registry that will back
`list_projects()` with real per-project metadata (mode, coverage threshold,
PR mode overrides) is Phase 7 -- for now, project existence and status are
derived entirely from checkpointer state, which is why they survive a
process restart without any registry file at all.
"""

import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.build import DEFAULT_GLOBAL_COST_THRESHOLD_USD, build_project_graph
from services.orchestrator.graph.state import new_project_state
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.plugins.discovery import LoadedPlugin
from services.orchestrator.progress_buffer import add_progress, clear_progress
from services.orchestrator.registry.validation import validate_project_id
from services.orchestrator.stacks import StacksConfig

_TERMINAL_PHASES = frozenset({"done", "blocked"})
_ACTIVE_PROJECT_FILENAME = ".active_project_id"

logger = logging.getLogger(__name__)


class UnknownProjectError(Exception):
    """Raised when a request references a project_id with no checkpoint history."""


class OrchestratorService:
    """Owns the compiled project subgraph and maps requests onto project threads."""

    def __init__(
        self,
        checkpointer: BaseCheckpointSaver,
        router: ModelRouter,
        executor_client: ExecutorClient,
        memory_conn: sqlite3.Connection,
        projects_root: Path,
        stacks_config: StacksConfig,
        plugins: list[LoadedPlugin] | None = None,
        global_cost_threshold_usd: float = DEFAULT_GLOBAL_COST_THRESHOLD_USD,
    ) -> None:
        """Builds the service.

        Args:
            checkpointer: The checkpoint saver backing every project thread.
            router: The ModelRouter every LLM-backed node (Planner, Coder,
                and later Reviewer/Tester) resolves its role from.
            executor_client: The Executor client the Coder dispatches
                read_file/write_file/run_command actions through.
            memory_conn: An open global memory connection for the Memory Curator.
            projects_root: The projects/ root directory for per-project LEARNINGS.md.
            stacks_config: The loaded stack registry (ADR-045) static_checks
                runs its deterministic commands from.
            plugins: Discovered plugins (services/orchestrator/plugins/discovery.py),
                threaded straight through to build_project_graph. None or an
                empty list wires the graph exactly as before this feature
                existed.
            global_cost_threshold_usd: The global weekly USD cost-gate
                threshold (TODO.md Phase 18); defaults to
                DEFAULT_GLOBAL_COST_THRESHOLD_USD. A real deployment passes
                OrchestratorConfig.global_cost_threshold_usd.
        """
        self._checkpointer = checkpointer
        self._projects_root = projects_root
        self._graph = build_project_graph(
            checkpointer, router, executor_client, memory_conn, projects_root,
            stacks_config, plugins, global_cost_threshold_usd,
        )

    def _invoke_graph(self, input_: Any, project_id: str) -> None:
        """Invokes the compiled graph for project_id, making a crash visible.

        A node raising (e.g. a local model returning malformed tool-call
        JSON deep inside LangChain's client) previously propagated past this
        service with no project-tagged log line anywhere, and no signal to
        anyone watching the project's progress. This logs the crash with a
        full traceback, records a "crashed" progress event any active
        ProgressPoller will pick up on its next GET /stream poll (safe from
        the clear_progress race a terminal-phase event would hit, since a
        crash never flips `phase` into a terminal value), and re-raises
        unchanged -- every caller's existing exception handling (api.py's
        HTTPException mapping, the Gateway's 5xx guard in
        _relay_webhook_result) is untouched.

        Args:
            input_: The value passed to `_graph.invoke` -- an initial
                ProjectState, a `Command(resume=...)`, or None to continue
                from the last checkpoint.
            project_id: The project thread being invoked.
        """
        try:
            self._graph.invoke(input_, config=self._config(project_id))
        except Exception as exc:
            logger.exception("project %s: graph run crashed", project_id)
            add_progress(project_id, f"❌ crashed: {type(exc).__name__}: {exc}")
            raise

    def handle_webhook(
        self, project_id: str | None, text: str, force_new: bool = False
    ) -> dict[str, Any]:
        """Starts a new project thread, resumes an active interview, or
        echoes status for an existing one.

        When project_id is None and force_new is False, the active-project
        pointer (ADR: 2026-07-25 Telegram conversational loop) decides what
        happens: a live, non-terminal active project paused mid-interview
        (pending_approval["kind"] == "plan_questions") has text fed in via
        Command(resume=text); a live, non-terminal active project waiting on
        anything else (mid-execution, or paused at a button gate) is left
        untouched and its status is returned with text_applied forced to
        False, so the text is never silently dropped without a signal.

        Args:
            project_id: The project this message belongs to, or None to let
                the active-project pointer (or force_new) decide.
            text: The free-text message body. Seeds a new project's intake
                interview, or answers the active project's current
                clarifying-question round.
            force_new: If True, always start a fresh project, ignoring any
                active project. Set by the Gateway's /new handler.

        Returns:
            The resulting project's status dict (see `status()`), with a
            `text_applied: False` key added only when text was received but
            not applied to any project.

        Raises:
            InvalidProjectIdError: If a caller-supplied project_id for a new
                project isn't a safe, single path-segment slug. The registry
                validates IDs it creates, but this entrypoint accepts
                arbitrary caller input that later becomes a filesystem path
                component (the Memory Curator's `projects/<id>/memory/`
                writes), so it must apply the same allow-list itself.
        """
        if project_id is not None:
            if not self._project_exists(project_id):
                validate_project_id(project_id)
                initial_state = new_project_state(project_id, initial_message=text)
                self._invoke_graph(initial_state, project_id)
            self._write_active_project_id(project_id)
            return self.status(project_id)

        if not force_new:
            active_id = self._read_active_project_id()
            if active_id is not None and self._project_exists(active_id):
                active_status = self.status(active_id)
                if active_status["phase"] not in _TERMINAL_PHASES:
                    pending = active_status["pending_approval"]
                    if pending is not None and pending.get("kind") == "plan_questions":
                        self._invoke_graph(Command(resume=text), active_id)
                        self._write_active_project_id(active_id)
                        return self.status(active_id)
                    return {**active_status, "text_applied": False}

        new_id = uuid.uuid4().hex[:12]
        initial_state = new_project_state(new_id, initial_message=text)
        self._invoke_graph(initial_state, new_id)
        self._write_active_project_id(new_id)
        return self.status(new_id)

    def resume(self, project_id: str, decision: str) -> dict[str, Any]:
        """Resumes a paused project thread with a human decision.

        Args:
            project_id: The project whose interrupt is being resolved.
            decision: The decision string -- "approve"/"reject"/"edit" for
                every interrupt gate in this graph (see
                services/orchestrator/graph/nodes.py), or a stakeholder's
                free-text answer while paused mid-interview.

        Returns:
            The resulting project's status dict (see `status()`).

        Raises:
            UnknownProjectError: If project_id has no checkpoint history.
        """
        if not self._project_exists(project_id):
            raise UnknownProjectError(project_id)
        self._write_active_project_id(project_id)
        self._invoke_graph(Command(resume=decision), project_id)
        return self.status(project_id)

    def activate(self, project_id: str) -> None:
        """Marks project_id active, and resumes its execution if it's stuck mid-flight.

        A project mid-task-loop (non-terminal, no pending_approval) with no
        further progress happening isn't waiting on anything -- it's stuck
        because whatever last called `_graph.invoke()` never returned, most
        often because the Orchestrator process itself stopped (e.g. a Docker
        stack restart) partway through. The checkpointer persists state after
        every completed superstep, but nothing else re-invokes the compiled
        graph after a crash. Passing `None` as input, rather than an initial
        state or `Command(resume=...)`, tells LangGraph to continue running
        from wherever the last checkpoint left off.

        A project currently paused at a real `interrupt()` (pending_approval
        is not None) is left untouched here -- that needs an explicit
        decision via `resume()`, not a bare continuation.

        Buffered progress is discarded before the continuation runs. What's
        left in the buffer describes the attempt that already failed --
        including the `❌ crashed:` event `_invoke_graph` records -- and the
        buffer is otherwise only ever cleared on a terminal-phase `status()`
        read. A poller that starts fresh after this (a restarted Gateway
        resets its own `since_seq`) would otherwise replay the old crash
        marker as if it had just happened, immediately ahead of the recovered
        run's real progress.

        Args:
            project_id: The project to activate (and resume, if stuck).

        Raises:
            UnknownProjectError: If project_id has no checkpoint history.
        """
        if not self._project_exists(project_id):
            raise UnknownProjectError(project_id)
        self._write_active_project_id(project_id)
        current = self.status(project_id)
        if current["phase"] not in _TERMINAL_PHASES and current["pending_approval"] is None:
            clear_progress(project_id)
            self._invoke_graph(None, project_id)

    def status(self, project_id: str) -> dict[str, Any]:
        """Reads a project's current phase and any pending approval.

        Args:
            project_id: The project to look up.

        Returns:
            A dict with `project_id`, `phase`, `pending_approval`, and
            `task_progress` (None before task decomposition has run, else
            `{"done": int, "total": int, "tasks": list[dict]}`).

        Raises:
            UnknownProjectError: If project_id has no checkpoint history.
        """
        snapshot = self._graph.get_state(self._config(project_id))
        if not snapshot.values:
            raise UnknownProjectError(project_id)
        phase = snapshot.values["phase"]
        if phase in _TERMINAL_PHASES:
            clear_progress(project_id)
        task_queue = snapshot.values["task_queue"]
        task_progress = (
            {
                "done": sum(1 for task in task_queue if task["status"] == "done"),
                "total": len(task_queue),
                "tasks": task_queue,
            }
            if task_queue
            else None
        )
        return {
            "project_id": project_id,
            "phase": phase,
            "pending_approval": snapshot.values["pending_approval"],
            "task_progress": task_progress,
        }

    def list_projects(self) -> list[str]:
        """Lists every project_id with checkpoint history.

        Returns:
            A sorted list of distinct project_ids, derived from the
            checkpointer's own thread history (no separate registry yet).
        """
        thread_ids = {
            checkpoint_tuple.config["configurable"]["thread_id"]
            for checkpoint_tuple in self._checkpointer.list(None)
        }
        return sorted(thread_ids)

    def _project_exists(self, project_id: str) -> bool:
        snapshot = self._graph.get_state(self._config(project_id))
        return bool(snapshot.values)

    def _config(self, project_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": project_id}}

    def _active_project_path(self) -> Path:
        return self._projects_root / _ACTIVE_PROJECT_FILENAME

    def _read_active_project_id(self) -> str | None:
        """Reads the active-project pointer.

        Returns:
            The active project's id, or None if no pointer file exists yet
            (a fresh deployment, or one where no project has ever been
            created or resumed).
        """
        path = self._active_project_path()
        if not path.exists():
            return None
        content = path.read_text().strip()
        return content or None

    def _write_active_project_id(self, project_id: str) -> None:
        """Overwrites the active-project pointer with project_id.

        No locking: this is a single-operator system (ADR-002) with
        Telegram delivering one chat's messages in order, and a whole-file
        overwrite is atomic enough for this scope.
        """
        path = self._active_project_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(project_id)
