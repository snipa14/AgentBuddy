"""aiogram handlers: commands, free-text forwarding, and approval callbacks.

Handlers declare `orchestrator: OrchestratorClient` as a parameter; aiogram
injects it from the Dispatcher's workflow data (set once in main.py), so
these stay plain, directly callable functions -- no closures to unwrap in
tests.

Only `/new` and free text reach `POST /webhook` (both can legitimately start
or address a project's intake conversation). `/status` and `/projects` are
read-only lookups against `GET /status/{id}` / `GET /projects` -- forwarding
them to the webhook, as this module originally did, silently started a new
junk project (with the literal command text as its intake message, and a
real Planner LLM call) on every status check.
"""

import logging

import httpx
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ForceReply, Message

from services.telegram_gateway.orchestrator_client import OrchestratorClient
from services.telegram_gateway.progress_poller import ProgressPoller
from services.telegram_gateway.rendering import (
    CALLBACK_APPROVE,
    CALLBACK_EDIT,
    CALLBACK_REJECT,
    CALLBACK_REPLY,
    render_approval,
    render_project_list,
    render_project_status,
    render_task_progress,
)

logger = logging.getLogger(__name__)

router = Router(name="foundry_gateway")

_DECISION_ACKNOWLEDGMENTS = {
    CALLBACK_APPROVE: "Approved.",
    CALLBACK_REJECT: "Rejected.",
    CALLBACK_EDIT: "Edit requested.",
}


def _start_poller_from_response(poller: ProgressPoller, response, chat_id: int) -> None:
    """Starts live progress polling for the project a webhook response names.

    A 5xx response means the graph crashed rather than returning a normal
    ProjectStatusResponse -- its body isn't JSON, and there's nothing to
    poll for anyway, so this is a no-op rather than crashing on
    response.json() (see _relay_webhook_result's matching guard).

    Args:
        poller: The Gateway's shared ProgressPoller.
        response: The httpx.Response returned by OrchestratorClient.send_webhook.
        chat_id: The Telegram chat to relay this project's progress into.
    """
    if response.status_code >= 500:
        return
    project_id = response.json().get("project_id")
    if project_id:
        poller.start(project_id, chat_id)


async def _answer_server_error(message: Message) -> None:
    """Tells the user the Orchestrator returned a >=500 response.

    A 5xx means the Orchestrator hit an uncaught exception (e.g. a graph
    node crash, a provider error mid-tool-call) rather than returning the
    normal JSON body its caller expects; FastAPI's default error body for
    that isn't JSON at all, so every call site that reads a JSON field off
    an Orchestrator response must check for this before calling
    response.json(), or that call raises an unhandled JSONDecodeError
    instead of telling the user anything.

    Args:
        message: The incoming message to reply to.
    """
    await message.answer(
        "The Orchestrator hit an internal error handling that -- check its "
        "logs. The project may be stuck at the same point; /continue can "
        "retry it."
    )


async def _relay_webhook_result(message: Message, response) -> None:
    """Renders a webhook response: an explicit hint if the text wasn't
    applied to any project, an Approve/Reject/Edit or Reply keyboard if the
    project is now paused at a gate, a task-progress summary if tasks
    exist, a plain acknowledgment otherwise.

    A 5xx response means the graph run itself crashed (an uncaught
    exception inside a node -- e.g. a provider error mid-tool-call) rather
    than returning a normal ProjectStatusResponse; FastAPI's default error
    body for that isn't JSON, so calling response.json() on it raised an
    unhandled JSONDecodeError here instead of telling the user anything.

    Args:
        message: The incoming message to reply to.
        response: The httpx.Response returned by OrchestratorClient.send_webhook.
    """
    if response.status_code >= 500:
        await _answer_server_error(message)
        return
    body = response.json()
    if body.get("text_applied") is False:
        await message.answer(
            f"Project {body['project_id']} is at phase '{body['phase']}' and isn't "
            "waiting on typed replies right now -- use the buttons above, or /status."
        )
        return
    pending_approval = body.get("pending_approval")
    if pending_approval:
        text, keyboard = render_approval(pending_approval)
        await message.answer(text, reply_markup=keyboard)
        return
    task_progress = body.get("task_progress")
    if task_progress:
        await message.answer(render_task_progress(task_progress))
        return
    phase = body.get("phase")
    if phase in ("done", "blocked"):
        await message.answer(f"Project {body.get('project_id')} is {phase}.")
        return
    await message.answer("Got it -- working on that.")


async def _answer_timeout_hint(message: Message) -> None:
    """Tells the user their message was received but the Orchestrator hasn't
    replied within the Gateway's client timeout. The graph run keeps going
    server-side regardless -- POST /webhook runs it synchronously to the next
    interrupt and a slow local-model chain (e.g. interview -> plan ->
    critique) can outlast the client's read timeout -- so this isn't a
    failure, just a slow turn.
    """
    await message.answer(
        "Still working on that -- a local-model turn can take a while, "
        "especially mid-interview. Check /status in a bit."
    )


@router.message(Command("start"))
async def handle_start(message: Message) -> None:
    """Greets the user. Sends nothing to the Orchestrator."""
    await message.answer("Foundry is online. Send /new to start a project.")


@router.message(Command("new"))
async def handle_new(
    message: Message, orchestrator: OrchestratorClient, poller: ProgressPoller
) -> None:
    """Forwards a /new command to the Orchestrator to start a project."""
    try:
        response = await orchestrator.send_webhook(
            message.from_user.id, message.text, force_new=True
        )
    except httpx.TimeoutException:
        await _answer_timeout_hint(message)
        return
    _start_poller_from_response(poller, response, message.chat.id)
    await _relay_webhook_result(message, response)


@router.message(Command("status"))
async def handle_status(message: Message, orchestrator: OrchestratorClient) -> None:
    """Reports one project's phase via GET /status/{project_id}.

    `/status <project_id>` looks that project up; a bare `/status` lists the
    known project ids to pick from instead of guessing. Either lookup can
    return a >=500 from the Orchestrator (see _answer_server_error), in
    which case there's no JSON body to read a field out of.
    """
    parts = (message.text or "").split(maxsplit=1)
    project_id = parts[1].strip() if len(parts) > 1 else ""
    if not project_id:
        projects_response = await orchestrator.get_projects()
        if projects_response.status_code >= 500:
            await _answer_server_error(message)
            return
        project_ids = projects_response.json().get("project_ids", [])
        await message.answer(
            "Usage: /status <project_id>\n" + render_project_list(project_ids)
        )
        return

    response = await orchestrator.get_status(project_id)
    if response.status_code == 404:
        await message.answer(f"Unknown project '{project_id}'. Try /projects.")
        return
    if response.status_code >= 500:
        await _answer_server_error(message)
        return
    await message.answer(render_project_status(response.json()))


@router.message(Command("continue"))
async def handle_continue(message: Message, orchestrator: OrchestratorClient) -> None:
    """Reactivates a project by id and shows whatever it's currently waiting on.

    Fallback for when the original approval/Reply message is gone -- e.g.
    after a Docker stack restart, or scrolled out of reach -- and there's no
    button left to tap. POST /activate/{project_id} only ever writes the
    active-project pointer file (ARCHITECTURE.md's webhook contract), so it
    works regardless of what the Gateway process itself remembers; this
    command is the only text-based way to reach it. It also now resumes a
    project's execution from checkpoint if it was stuck mid-flight
    (OrchestratorService.activate), which can run the LangGraph synchronously
    just like /webhook and /resume do -- so this can be slow against a local
    model the same way those are.
    """
    parts = (message.text or "").split(maxsplit=1)
    project_id = parts[1].strip() if len(parts) > 1 else ""
    if not project_id:
        await message.answer("Usage: /continue <project_id>")
        return
    try:
        response = await orchestrator.activate(project_id)
    except httpx.TimeoutException:
        await _answer_timeout_hint(message)
        return
    if response.status_code == 404:
        await message.answer(f"Unknown project '{project_id}'. Try /projects.")
        return
    await _relay_webhook_result(message, response)


@router.message(Command("projects"))
async def handle_projects(message: Message, orchestrator: OrchestratorClient) -> None:
    """Lists every known project id via GET /projects.

    A >=500 from the Orchestrator has no JSON body in the expected shape
    (see _answer_server_error).
    """
    response = await orchestrator.get_projects()
    if response.status_code >= 500:
        await _answer_server_error(message)
        return
    await message.answer(render_project_list(response.json().get("project_ids", [])))


@router.message(F.text)
async def handle_free_text(
    message: Message, orchestrator: OrchestratorClient, poller: ProgressPoller
) -> None:
    """Forwards any other free-text message to the Orchestrator."""
    try:
        response = await orchestrator.send_webhook(message.from_user.id, message.text)
    except httpx.TimeoutException:
        await _answer_timeout_hint(message)
        return
    _start_poller_from_response(poller, response, message.chat.id)
    await _relay_webhook_result(message, response)


@router.callback_query(F.data.startswith((CALLBACK_APPROVE, CALLBACK_REJECT, CALLBACK_EDIT)))
async def handle_approval_callback(
    callback: CallbackQuery, orchestrator: OrchestratorClient, poller: ProgressPoller
) -> None:
    """Resolves an Approve/Reject/Edit button press via POST /resume/{thread_id}.

    The tap is acknowledged and the keyboard cleared *before* the resume
    call, not after: POST /resume runs the LangGraph synchronously and can
    take well over a minute against a local model, but Telegram invalidates
    a callback query's answer token long before that -- calling
    callback.answer() after a slow resume() raised "query is too old" in
    production, crashing the handler before the keyboard was ever cleared.

    The resume response is relayed back into the chat once it returns: a
    project that advances straight to its next gate (plan approval -> merge
    approval) gets that gate's keyboard pushed immediately, instead of
    stalling silently until the user happens to send another message.

    Progress polling is (re)started off the resume response too. A poller
    started at `/new` has usually stopped by the time a human taps Approve
    on a later gate, and every gate this graph has requires a human tap
    (.claude/rules/git.md's merge gate always does), so without this nothing
    watches the execution that the approval just unblocked -- and the
    terminal done/blocked notice never arrives.
    """
    decision, thread_id = callback.data.split(":", 1)
    await callback.answer(_DECISION_ACKNOWLEDGMENTS.get(decision, "Done."))
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    try:
        response = await orchestrator.resume(thread_id, decision)
    except httpx.TimeoutException:
        if callback.message is not None:
            await _answer_timeout_hint(callback.message)
        return
    if callback.message is None:
        return
    _start_poller_from_response(poller, response, callback.message.chat.id)
    await _relay_webhook_result(callback.message, response)


@router.callback_query(F.data.startswith(CALLBACK_REPLY))
async def handle_reply_callback(callback: CallbackQuery, orchestrator: OrchestratorClient) -> None:
    """Activates the tapped project and opens a force-reply text box.

    The Reply button on an interview-question message is the disambiguator
    for when more than one project is paused at once (the common
    single-project case never needs it -- see docs/superpowers/specs/
    2026-07-25-telegram-conversational-loop-design.md).

    The tap is acknowledged and the keyboard cleared *before* calling
    activate(), not after: activate() can now resume a stuck project's
    execution from its checkpoint (OrchestratorService.activate), which runs
    the LangGraph synchronously just like resume() does -- the same "query
    is too old" crash fixed in handle_approval_callback applies here too if
    an old Reply button gets tapped on a project that's since moved past its
    interview into a long-running implementation.

    A stale Reply button can also be tapped on a project that's since been
    deleted or renamed; activate() 404s for that the same way it does in
    handle_continue, and this must not fall through to the ForceReply prompt
    with nothing valid left to receive the reply.
    """
    _, project_id = callback.data.split(":", 1)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_reply_markup(reply_markup=None)
    try:
        response = await orchestrator.activate(project_id)
    except httpx.TimeoutException:
        if callback.message is not None:
            await _answer_timeout_hint(callback.message)
        return
    if callback.message is None:
        return
    if response.status_code == 404:
        await callback.message.answer(f"Unknown project '{project_id}'. Try /projects.")
        return
    await callback.message.answer(
        "Type your answer:", reply_markup=ForceReply(selective=True)
    )
