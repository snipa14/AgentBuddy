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

import uuid
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from services.orchestrator.graph.build import build_project_graph
from services.orchestrator.graph.state import new_project_state


class UnknownProjectError(Exception):
    """Raised when a request references a project_id with no checkpoint history."""


class OrchestratorService:
    """Owns the compiled project subgraph and maps requests onto project threads."""

    def __init__(self, checkpointer: BaseCheckpointSaver) -> None:
        """Builds the service.

        Args:
            checkpointer: The checkpoint saver backing every project thread.
        """
        self._checkpointer = checkpointer
        self._graph = build_project_graph(checkpointer)

    def handle_webhook(self, project_id: str | None, text: str) -> dict[str, Any]:
        """Starts a new project thread, or echoes status for an existing one.

        Phase 5 has no Planner agent yet to interpret free text against an
        in-progress thread, so a webhook naming a known project_id just
        returns that project's current status without feeding the text into
        the graph -- Phase 8 (Planner/Intake Agent) is what makes free text
        actually drive an existing thread forward.

        Args:
            project_id: The project this message belongs to, or None/unknown
                to start a new project.
            text: The free-text message body. Unused until Phase 8.

        Returns:
            The resulting project's status dict (see `status()`).
        """
        del text
        if project_id is None or not self._project_exists(project_id):
            project_id = project_id or uuid.uuid4().hex[:12]
            self._graph.invoke(new_project_state(project_id), config=self._config(project_id))
        return self.status(project_id)

    def resume(self, project_id: str, decision: str) -> dict[str, Any]:
        """Resumes a paused project thread with a human decision.

        Args:
            project_id: The project whose interrupt is being resolved.
            decision: The decision string (e.g. "continue"/"pause" for the
                cost gate, "approve"/"reject" for the merge gate).

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
            A dict with `project_id`, `phase`, and `pending_approval`.

        Raises:
            UnknownProjectError: If project_id has no checkpoint history.
        """
        snapshot = self._graph.get_state(self._config(project_id))
        if not snapshot.values:
            raise UnknownProjectError(project_id)
        return {
            "project_id": project_id,
            "phase": snapshot.values["phase"],
            "pending_approval": snapshot.values["pending_approval"],
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
