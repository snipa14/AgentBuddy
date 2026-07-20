"""Renders Orchestrator notifications as Telegram messages (ARCHITECTURE.md §4.1)."""

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_APPROVE = "approve"
CALLBACK_REJECT = "reject"
CALLBACK_EDIT = "edit"

_DEFAULT_TEXT = "Approval needed."


def render_approval(pending_approval: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Renders a pending_approval payload as message text plus an inline keyboard.

    Args:
        pending_approval: The payload attached to a paused graph's
            interrupt() call (ARCHITECTURE.md §5), carrying at least a
            "thread_id" and, usually, a human-readable "summary".

    Returns:
        A (text, keyboard) pair ready to pass to a Message's answer/reply,
        with Approve/Reject/Edit buttons whose callback_data encodes the
        decision and thread_id as "<decision>:<thread_id>".
    """
    thread_id = pending_approval["thread_id"]
    text = pending_approval.get("summary", _DEFAULT_TEXT)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Approve", callback_data=f"{CALLBACK_APPROVE}:{thread_id}"
                ),
                InlineKeyboardButton(
                    text="Reject", callback_data=f"{CALLBACK_REJECT}:{thread_id}"
                ),
                InlineKeyboardButton(text="Edit", callback_data=f"{CALLBACK_EDIT}:{thread_id}"),
            ]
        ]
    )
    return text, keyboard


def render_project_status(body: dict[str, Any]) -> str:
    """Renders a ProjectStatusResponse body as a short status message.

    Args:
        body: The decoded GET /status/{project_id} response
            (services/orchestrator/api.py's ProjectStatusResponse shape).

    Returns:
        "<project_id>: <phase>", plus render_task_progress's lines when the
        project has a task_queue.
    """
    lines = [f"{body['project_id']}: {body['phase']}"]
    task_progress = body.get("task_progress")
    if task_progress:
        lines.append(render_task_progress(task_progress))
    return "\n".join(lines)


def render_project_list(project_ids: list[str]) -> str:
    """Renders the GET /projects id list as a Telegram message.

    Args:
        project_ids: Every known project_id, already sorted by the
            Orchestrator.

    Returns:
        One project per line, or a hint when none exist yet.
    """
    if not project_ids:
        return "No projects yet. Send /new <description> to start one."
    return "Projects:\n" + "\n".join(f"- {project_id}" for project_id in project_ids)


def render_task_progress(task_progress: dict[str, Any]) -> str:
    """Renders a ProjectStatusResponse's task_progress field as a status line.

    Args:
        task_progress: {"done": int, "total": int, "tasks": [{"description",
            "status", "attempts"}, ...]} (services/orchestrator/api.py).

    Returns:
        A one- or two-line summary, e.g. "1/3 tasks done\\nIn progress: ..."
        -- a blocked task takes priority over an in-progress one for the
        second line, since a blocked task means the phase has actually halted.
    """
    lines = [f"{task_progress['done']}/{task_progress['total']} tasks done"]
    blocked = next(
        (task for task in task_progress["tasks"] if task["status"] == "blocked"), None
    )
    active = next(
        (task for task in task_progress["tasks"] if task["status"] == "in_progress"), None
    )
    if blocked:
        lines.append(f"Blocked: {blocked['description']}")
    elif active:
        lines.append(f"In progress: {active['description']}")
    return "\n".join(lines)
