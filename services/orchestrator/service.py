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
from services.orchestrator.progress_buffer import clear_progress

_TERMINAL_PHASES = frozenset({"done", "blocked"})


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
            global_cost_threshold_usd: The global weekly USD cost-gate
                threshold (TODO.md Phase 18); defaults to
                DEFAULT_GLOBAL_COST_THRESHOLD_USD. A real deployment passes
                OrchestratorConfig.global_cost_threshold_usd.
        """
        self._checkpointer = checkpointer
        self._graph = build_project_graph(
            checkpointer, router, executor_client, memory_conn, projects_root,
            global_cost_threshold_usd,
        )

    def handle_webhook(self, project_id: str | None, text: str) -> dict[str, Any]:
        """Starts a new project thread, or echoes status for an existing one.

        The Planner's intake interview (Phase 8) is only wired to drive a
        *brand-new* thread from its seeded initial message; a webhook naming
        a known project_id just returns that project's current status
        without feeding the text into the graph -- turning an in-progress
        thread's free text into a real interrupt-resume answer needs a
        richer webhook contract than "project_id + text" (which round it's
        answering, for one), left for whichever phase builds the full
        conversational Telegram loop.

        Args:
            project_id: The project this message belongs to, or None/unknown
                to start a new project.
            text: The free-text message body. Seeds the new project's
                intake interview when starting a new thread; ignored for an
                existing one.

        Returns:
            The resulting project's status dict (see `status()`).
        """
        if project_id is None or not self._project_exists(project_id):
            project_id = project_id or uuid.uuid4().hex[:12]
            initial_state = new_project_state(project_id, initial_message=text)
            self._graph.invoke(initial_state, config=self._config(project_id))
        return self.status(project_id)

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
        self._graph.invoke(Command(resume=decision), config=self._config(project_id))
        return self.status(project_id)

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
