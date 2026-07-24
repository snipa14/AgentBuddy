"""Per-project graph state schema (ARCHITECTURE.md §5)."""

from operator import add
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage

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
CoverageGateMode = Literal["hard", "soft", "configurable"]
Stack = str
TaskStatus = Literal["pending", "in_progress", "done", "blocked"]


class ProjectState(TypedDict):
    """The full state of a single project's LangGraph thread.

    `pending_approval` is what gets attached to an `interrupt()` payload --
    the Gateway reads it to render the right Telegram message (plan text
    plus approve/reject buttons, or a cost-threshold alert). Valid `stack`
    values are the keys of `config/stacks.yaml`, enforced at the CLI
    boundary (`registry/cli.py`), not by this schema.
    """

    project_id: str
    mode: Mode
    stack: Stack
    phase: Phase
    plan: dict
    messages: Annotated[list[BaseMessage], add]
    task_queue: list[dict]
    cost_usd_session: float
    cost_usd_weekly: float
    coverage_pct: float | None
    coverage_threshold: int
    pr_mode: PrMode
    coverage_gate_mode: CoverageGateMode
    git_branch: str | None
    git_remote_url: str | None
    curator_source: str | None
    messages_summary: str
    messages_curated_through: int
    changed_files: list[str]
    pending_approval: dict | None


def new_project_state(
    project_id: str,
    mode: Mode = "greenfield",
    stack: Stack = "python",
    coverage_threshold: int = 80,
    pr_mode: PrMode = "manual",
    coverage_gate_mode: CoverageGateMode = "hard",
    cost_usd_session: float = 0.0,
    cost_usd_weekly: float = 0.0,
    initial_message: str | None = None,
) -> ProjectState:
    """Builds the initial state for a brand-new project thread.

    Args:
        project_id: The new project's identifier; also used as its LangGraph
            thread_id.
        mode: Whether this is a greenfield or brownfield project.
        stack: The project's declared language/stack (ADR-028) -- selects
            the Executor's workspace image and cache volume for every tool
            call made on this project's behalf. Defaults to "python" since
            every project needs some value here and Python is Foundry's
            original, always-available stack; a real deployment should
            thread the registry's own `stack` field through instead (the
            same pre-existing registry-to-ProjectState wiring gap ADR-029
            already flagged for `git_remote_url`).
        coverage_threshold: Minimum test coverage percentage required to pass
            the testing phase (per-project override, ARCHITECTURE.md §4.6).
        pr_mode: How pull requests are handled once a project reaches the
            merge gate.
        coverage_gate_mode: How the Tester enforces the coverage gate --
            'hard' blocks below threshold, 'soft' warns but passes,
            'configurable' is reserved for a future per-project policy.
            Defaults to 'hard' (TODO.md Phase 11).
        cost_usd_session: Starting session cost in USD. Non-zero values are
            only used to drive tests of the cost-threshold interrupt.
        cost_usd_weekly: Starting weekly cost in USD. Non-zero values are
            only used to drive tests of the global cost-threshold gate
            (TODO.md Phase 18) -- a real deployment always starts this at
            0.0 and lets `_with_cost` accumulate it.
        initial_message: The stakeholder's original project request (e.g.
            the text of a Telegram `/new` message), seeded as the first
            entry in `messages` -- this is what the Planner's intake
            interview (Phase 8) interviews about. None leaves messages
            empty, for tests that don't exercise the Planner.

    Returns:
        A fully populated ProjectState with phase "intake".
    """
    return ProjectState(
        project_id=project_id,
        mode=mode,
        stack=stack,
        phase="intake",
        plan={},
        messages=[HumanMessage(content=initial_message)] if initial_message else [],
        task_queue=[],
        cost_usd_session=cost_usd_session,
        cost_usd_weekly=cost_usd_weekly,
        coverage_pct=None,
        coverage_threshold=coverage_threshold,
        pr_mode=pr_mode,
        coverage_gate_mode=coverage_gate_mode,
        git_branch=None,
        git_remote_url=None,
        curator_source=None,
        messages_summary="",
        messages_curated_through=0,
        changed_files=[],
        pending_approval=None,
    )
