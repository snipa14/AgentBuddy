"""Per-project graph state schema (ARCHITECTURE.md §5)."""

from operator import add
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage

Phase = Literal[
    "intake",
    "planning",
    "roasting",
    "awaiting_plan_approval",
    "implementing",
    "reviewing",
    "testing",
    "awaiting_cost_approval",
    "awaiting_merge_approval",
    "pr_pending",
    "done",
    "blocked",
]

Mode = Literal["greenfield", "brownfield"]
PrMode = Literal["auto", "manual", "configurable"]


class ProjectState(TypedDict):
    """The full state of a single project's LangGraph thread.

    `pending_approval` is what gets attached to an `interrupt()` payload --
    the Gateway reads it to render the right Telegram message (plan text
    plus approve/reject buttons, or a cost-threshold alert).
    """

    project_id: str
    mode: Mode
    phase: Phase
    plan: dict
    messages: Annotated[list[BaseMessage], add]
    task_queue: list[dict]
    current_task: dict | None
    cost_usd_session: float
    cost_usd_weekly: float
    coverage_pct: float | None
    coverage_threshold: int
    pr_mode: PrMode
    pending_approval: dict | None


def new_project_state(
    project_id: str,
    mode: Mode = "greenfield",
    coverage_threshold: int = 80,
    pr_mode: PrMode = "manual",
    cost_usd_session: float = 0.0,
) -> ProjectState:
    """Builds the initial state for a brand-new project thread.

    Args:
        project_id: The new project's identifier; also used as its LangGraph
            thread_id.
        mode: Whether this is a greenfield or brownfield project.
        coverage_threshold: Minimum test coverage percentage required to pass
            the testing phase (per-project override, ARCHITECTURE.md §4.6).
        pr_mode: How pull requests are handled once a project reaches the
            merge gate.
        cost_usd_session: Starting session cost in USD. Non-zero values are
            only used to drive tests of the cost-threshold interrupt.

    Returns:
        A fully populated ProjectState with phase "intake".
    """
    return ProjectState(
        project_id=project_id,
        mode=mode,
        phase="intake",
        plan={},
        messages=[],
        task_queue=[],
        current_task=None,
        cost_usd_session=cost_usd_session,
        cost_usd_weekly=0.0,
        coverage_pct=None,
        coverage_threshold=coverage_threshold,
        pr_mode=pr_mode,
        pending_approval=None,
    )
