"""Sums other projects' latest checkpointed weekly cost (TODO.md Phase 18).

The cost-threshold gate (ADR-009) is global, not per-project (TODO.md
Phase 18) -- node_cost_check needs every *other* project's most recent
cost_usd_weekly, added to the current project's own live value. Takes the
checkpointer as a duck-typed `Any` rather than importing
`langgraph.checkpoint.base.BaseCheckpointSaver` for the type hint, so this
module (and its unit tests) stay importable without langgraph installed --
every real caller (graph/build.py) already has langgraph as a hard
dependency anyway.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def other_projects_cost_usd_weekly(checkpointer: Any, exclude_project_id: str) -> float:
    """Sums the latest checkpointed cost_usd_weekly across every project but one.

    `checkpointer.list(None)` returns every historical checkpoint *version*
    for every thread, not just the latest one per thread -- summing across
    all of them would count one project's spend as many times as it has
    checkpoints. This walks the distinct thread_ids once, then fetches only
    each thread's latest checkpoint via `get_tuple()`.

    Args:
        checkpointer: A BaseCheckpointSaver-shaped object (duck-typed, see
            module docstring): `.list(None)` yields objects with
            `.config["configurable"]["thread_id"]`, and `.get_tuple(config)`
            returns the latest such object (or None) for one thread_id.
        exclude_project_id: The project_id to omit from the sum -- callers
            pass the current project's own project_id and add its live
            `state["cost_usd_weekly"]` themselves, since that's fresher
            than whatever's already checkpointed for it.

    Returns:
        The sum of every other known project's latest `cost_usd_weekly`,
        defaulting an individual project's contribution to 0.0 if its
        latest checkpoint has no `cost_usd_weekly` channel value yet.
    """
    thread_ids = {
        checkpoint_tuple.config["configurable"]["thread_id"]
        for checkpoint_tuple in checkpointer.list(None)
    } - {exclude_project_id}

    total = 0.0
    for thread_id in thread_ids:
        latest = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        if latest is None:
            continue
        total += latest.checkpoint["channel_values"].get("cost_usd_weekly", 0.0)
    return total
