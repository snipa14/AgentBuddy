"""Background poller relaying live Coder progress into Telegram (ADR-027).

Started once per project the moment its `/new`/webhook flow begins
(`bot.py`'s handlers), this polls the Orchestrator's `GET /stream/{project_id}`
endpoint every few seconds and relays whatever new chunks it finds as a
single, repeatedly-edited Telegram message per project -- never one message
per chunk. Chunks are discrete progress events (tool dispatch/outcome, task
outcomes, crash notices), so they're joined with newlines, one event per
line, rather than concatenated as the raw content deltas this originally
relayed.

Polling for a project stops once it reaches a terminal phase (`done`/
`blocked`, read via `GET /status/{project_id}`) or has gone idle (no new
chunks, still not terminal) for too many consecutive polls. Either way the
poller appends a final line to the project's message before it stops: a
terminal notice (`✅ project done` / `🚫 project blocked: <reason>`, ADR-052)
when the phase went terminal, or a give-up notice when the idle budget ran
out. A project parked at a human approval gate is not idle in that sense --
its countdown is reset on every poll that sees a `pending_approval`, so a
gate waiting on a human tap never triggers the give-up path.
"""

import asyncio
import logging

from aiogram import Bot

from services.telegram_gateway.orchestrator_client import OrchestratorClient

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 3.0
MAX_IDLE_POLLS = 100
TERMINAL_PHASES = frozenset({"done", "blocked"})
TELEGRAM_MESSAGE_LIMIT = 4096
_TRUNCATION_MARKER = "... [truncated -- showing latest progress] ...\n"
_GIVE_UP_MESSAGE = (
    "⚠️ Lost track of this project -- no updates in a while. "
    "Check /status or /continue."
)


def _truncate_for_telegram(text: str) -> str:
    """Keeps text within Telegram's message-length limit, favoring the latest content.

    Args:
        text: The full progress text accumulated for a project so far.

    Returns:
        text unchanged if it already fits; otherwise its tail, prefixed with
        a truncation marker, sized to fit within TELEGRAM_MESSAGE_LIMIT.
    """
    if len(text) <= TELEGRAM_MESSAGE_LIMIT:
        return text
    tail_budget = TELEGRAM_MESSAGE_LIMIT - len(_TRUNCATION_MARKER)
    return _TRUNCATION_MARKER + text[-tail_budget:]


def _terminal_message(phase: str, task_progress: dict | None = None) -> str:
    """Builds the final relayed line once a project reaches a terminal phase.

    A blocked project names the blocked task's description as its reason,
    read from the `task_progress` block `GET /status/{project_id}` already
    returns. A project blocked by something other than a task -- a rejected
    cost gate or merge gate -- has no blocked task to name, so it falls back
    to the bare notice rather than inventing a reason.

    Args:
        phase: The project's terminal phase, "done" or "blocked".
        task_progress: The status response's `task_progress` block, or None
            if the project never got as far as task decomposition.

    Returns:
        A short status line to append to the project's relayed message.
    """
    if phase == "done":
        return "✅ project done"
    tasks = (task_progress or {}).get("tasks") or []
    reasons = [
        task["description"] for task in tasks if task.get("status") == "blocked"
    ]
    if not reasons:
        return "🚫 project blocked"
    return f"🚫 project blocked: {'; '.join(reasons)}"


class ProgressPoller:
    """Owns one background poll loop per actively-streaming project."""

    def __init__(
        self,
        orchestrator: OrchestratorClient,
        bot: Bot,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
        max_idle_polls: int = MAX_IDLE_POLLS,
    ) -> None:
        """Builds the poller.

        Args:
            orchestrator: The Orchestrator client to poll /stream and /status
                through.
            bot: The aiogram Bot used to send/edit progress messages.
            poll_interval_seconds: Seconds to wait between polls of the same
                project.
            max_idle_polls: Consecutive polls with no new chunks (and no
                terminal phase) before a project's poll loop gives up.
        """
        self._orchestrator = orchestrator
        self._bot = bot
        self._poll_interval_seconds = poll_interval_seconds
        self._max_idle_polls = max_idle_polls
        self._tasks: dict[str, asyncio.Task] = {}
        self._since_seq: dict[str, int] = {}
        self._idle_polls: dict[str, int] = {}
        self._messages: dict[str, tuple[int, str]] = {}

    def start(self, project_id: str, chat_id: int) -> None:
        """Starts a background poll loop for project_id, unless one is already running.

        Args:
            project_id: The project to poll progress for.
            chat_id: The Telegram chat to relay progress messages into.
        """
        existing = self._tasks.get(project_id)
        if existing is not None and not existing.done():
            return
        self._since_seq[project_id] = -1
        self._idle_polls[project_id] = 0
        self._tasks[project_id] = asyncio.create_task(self._poll_loop(project_id, chat_id))

    async def _poll_loop(self, project_id: str, chat_id: int) -> None:
        while await self._poll_once_resilient(project_id, chat_id):
            await asyncio.sleep(self._poll_interval_seconds)

    async def _poll_once_resilient(self, project_id: str, chat_id: int) -> bool:
        """Runs one poll cycle, surviving transient failures.

        A momentarily-unreachable Orchestrator or a rejected Telegram edit
        must not kill the poll task silently (an asyncio.Task's exception is
        only ever seen at GC time) -- a failed cycle logs, counts as an idle
        poll, and lets the loop try again, so the idle budget still bounds
        how long a persistently-broken project keeps polling.

        Returns:
            True if polling should continue, False once this project's poll
            loop should stop (terminal phase reached, or idle timeout hit).
        """
        try:
            return await self._poll_once(project_id, chat_id)
        except Exception:
            logger.warning("project %s: progress poll failed, retrying", project_id, exc_info=True)
            self._idle_polls[project_id] = self._idle_polls.get(project_id, 0) + 1
            if self._idle_polls[project_id] >= self._max_idle_polls:
                try:
                    await self._relay(project_id, chat_id, _GIVE_UP_MESSAGE)
                except Exception:
                    logger.warning(
                        "project %s: failed to send give-up message", project_id, exc_info=True
                    )
                self._stop(project_id)
                return False
            return True

    async def _poll_once(self, project_id: str, chat_id: int) -> bool:
        """Runs one poll cycle for project_id.

        Relays any new progress events, then reads the project's phase to
        decide whether to keep polling. A terminal phase gets a final
        done/blocked line relayed before the loop stops; an exhausted idle
        budget gets a give-up line instead. A project parked at a human
        approval gate resets its idle budget rather than spending it -- it's
        waiting on exactly what it's supposed to wait on, not stuck.

        Returns:
            True if polling should continue, False once this project's poll
            loop should stop (terminal phase reached, or idle timeout hit).
        """
        stream_response = await self._orchestrator.stream(project_id, self._since_seq[project_id])
        chunks = stream_response.json().get("chunks", [])
        if chunks:
            self._since_seq[project_id] = max(chunk["seq"] for chunk in chunks)
            self._idle_polls[project_id] = 0
            await self._relay(project_id, chat_id, "\n".join(chunk["text"] for chunk in chunks))
        else:
            self._idle_polls[project_id] += 1

        status_response = await self._orchestrator.get_status(project_id)
        body = status_response.json()
        phase = body.get("phase")
        if body.get("pending_approval") is not None:
            self._idle_polls[project_id] = 0
        if phase in TERMINAL_PHASES:
            try:
                await self._relay(
                    project_id, chat_id, _terminal_message(phase, body.get("task_progress"))
                )
            except Exception:
                logger.warning(
                    "project %s: failed to send terminal message", project_id, exc_info=True
                )
            self._stop(project_id)
            return False
        if self._idle_polls[project_id] >= self._max_idle_polls:
            await self._relay(project_id, chat_id, _GIVE_UP_MESSAGE)
            self._stop(project_id)
            return False
        return True

    async def _relay(self, project_id: str, chat_id: int, text: str) -> None:
        """Sends or edits this project's single progress message with new text.

        The full, untruncated text is what's kept in `_messages` and grown on
        each call -- only what's actually sent to Telegram is truncated to
        Telegram's own message-length limit, always keeping the most recent
        (most relevant) progress rather than the oldest.

        Each relay lands on its own line: what arrives here is a batch of
        discrete progress events, not a continuation of the previous batch's
        sentence, so the separator belongs here rather than at each of the
        Orchestrator's many `add_progress` call sites.
        """
        existing = self._messages.get(project_id)
        previous_text = existing[1] if existing is not None else ""
        combined = f"{previous_text}\n{text}" if previous_text else text

        if existing is None:
            sent = await self._bot.send_message(chat_id, _truncate_for_telegram(combined))
            self._messages[project_id] = (sent.message_id, combined)
            return

        message_id, _ = existing
        await self._bot.edit_message_text(
            _truncate_for_telegram(combined), chat_id=chat_id, message_id=message_id
        )
        self._messages[project_id] = (message_id, combined)

    def _stop(self, project_id: str) -> None:
        self._tasks.pop(project_id, None)
        self._since_seq.pop(project_id, None)
        self._idle_polls.pop(project_id, None)
        self._messages.pop(project_id, None)
