"""Renders Orchestrator notifications as Telegram messages (ARCHITECTURE.md §4.1)."""

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_APPROVE = "approve"
CALLBACK_REJECT = "reject"
CALLBACK_EDIT = "edit"
CALLBACK_REPLY = "reply"
CALLBACK_ACTIVATE = "activate"

_DEFAULT_TEXT = "Approval needed."


def render_approval(pending_approval: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Renders a pending_approval payload as message text plus an inline keyboard.

    Args:
        pending_approval: The payload attached to a paused graph's
            interrupt() call (ARCHITECTURE.md §5), carrying at least a
            "thread_id" and, usually, a human-readable "summary".

    Returns:
        A (text, keyboard) pair ready to pass to a Message's answer/reply.
        A "plan_questions" kind (the Planner's clarifying-question
        interview) gets a single Reply button, since free text is how it's
        actually answered -- see docs/superpowers/specs/
        2026-07-25-telegram-conversational-loop-design.md. Every other kind
        keeps the Approve/Reject/Edit keyboard, whose callback_data encodes
        the decision and thread_id as "<decision>:<thread_id>".
    """
    thread_id = pending_approval["thread_id"]
    text = pending_approval.get("summary", _DEFAULT_TEXT)
    if pending_approval.get("kind") == "plan_questions":
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Reply", callback_data=f"{CALLBACK_REPLY}:{thread_id}")]
            ]
        )
        return text, keyboard
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Approve", callback_data=f"{CALLBACK_APPROVE}:{thread_id}"
                ),
                InlineKeyboardButton(text="Reject", callback_data=f"{CALLBACK_REJECT}:{thread_id}"),
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
        "<name> (<project_id>): <phase>" when the project has a registered
        name, else "<project_id>: <phase>", with " (active)" appended when
        this is the project a bare free-text reply currently routes to,
        plus render_task_progress's lines when the project has a task_queue.
    """
    label = _project_label(body["project_id"], body.get("name"))
    active_suffix = " (active)" if body.get("active") else ""
    cancelled_suffix = " (cancelled)" if body.get("cancelled") else ""
    lines = [f"{label}: {body['phase']}{active_suffix}{cancelled_suffix}"]
    task_progress = body.get("task_progress")
    if task_progress:
        lines.append(render_task_progress(task_progress))
    return "\n".join(lines)


def render_project_list(projects: list[dict[str, Any]]) -> str:
    """Renders the GET /projects list as a Telegram message.

    Args:
        projects: Every known project as {"id": project_id, "name":
            name-or-None, "active": bool} dicts (services/orchestrator/
            api.py's ProjectSummary shape), already sorted by id by the
            Orchestrator.

    Returns:
        One project per line ("<name> (<id>)", or bare "<id>" when
        unnamed), with a "-> " marker on the active project (the one a
        bare free-text reply currently routes to), or a hint when none
        exist yet.
    """
    if not projects:
        return "No projects yet. Send /new <description> to start one."
    return "Projects:\n" + "\n".join(
        f"{'-> ' if project.get('active') else '- '}"
        f"{_project_label(project['id'], project.get('name'))}"
        f"{' (cancelled)' if project.get('cancelled') else ''}"
        for project in projects
    )


def render_project_buttons(projects: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    """Builds a tap-to-activate button per project, for /projects' reply_markup.

    Switching projects otherwise means retyping a name or id exactly
    (typo-prone) after reading it off the list -- each button's
    callback_data reuses the same "activate:<id>" scheme /continue already
    drives through OrchestratorService.activate(), so tapping a row does
    exactly what typing /continue <id> would have.

    Args:
        projects: Every known project as {"id": project_id, "name":
            name-or-None, "active": bool} dicts (services/orchestrator/
            api.py's ProjectSummary shape).

    Returns:
        An InlineKeyboardMarkup with one row per project (the active
        project's button prefixed "-> "), or None when there are no
        projects to show buttons for.
    """
    if not projects:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=(
                        f"{'-> ' if project.get('active') else ''}"
                        f"{_project_label(project['id'], project.get('name'))}"
                        f"{' (cancelled)' if project.get('cancelled') else ''}"
                    ),
                    callback_data=f"{CALLBACK_ACTIVATE}:{project['id']}",
                )
            ]
            for project in projects
        ]
    )


def _project_label(project_id: str, name: str | None) -> str:
    return f"{name} ({project_id})" if name else project_id


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
    blocked = next((task for task in task_progress["tasks"] if task["status"] == "blocked"), None)
    active = next(
        (task for task in task_progress["tasks"] if task["status"] == "in_progress"), None
    )
    if blocked:
        lines.append(f"Blocked: {blocked['description']}")
    elif active:
        lines.append(f"In progress: {active['description']}")
    return "\n".join(lines)
