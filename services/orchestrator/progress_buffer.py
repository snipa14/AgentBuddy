"""In-memory per-project progress-chunk buffer for live Telegram streaming (ADR-027).

Holds discrete progress events -- a tool dispatch and its success/failure
(`agents/coder.py`'s tool wrappers), a task's outcome (`graph/nodes.py`'s
`node_coder`), a crashed graph run (`service.py`) -- pushed by several
producers as they happen, read by the Orchestrator's
`GET /stream/{project_id}` endpoint (`api.py`), and polled by the Telegram
Gateway (`telegram_gateway/progress_poller.py`), which renders one event per
line. This originally carried raw content deltas streamed from a single
producer (the Coder's own turn), which proved unreadable relayed into
Telegram and told a watcher nothing about what the agent was actually
doing. Chunks are addressed by a
per-project monotonic sequence number so a poller can ask for only what it
hasn't seen yet (`since_seq`) instead of the buffer needing to destroy state
on every read -- a slow or duplicate poll never loses or re-sends a chunk.

This buffer is deliberately process-local, in-memory, best-effort state --
not part of `ProjectState` and not checkpointed. Losing it on a process
restart only means a few seconds of missed incremental progress text, never
a missed state transition (the graph's own checkpointed state is what every
gate and phase transition actually depends on).
"""

import threading
from dataclasses import dataclass

_lock = threading.Lock()
_buffers: dict[str, list[str]] = {}


@dataclass
class ProgressChunk:
    """One recorded progress event, addressed by its position in the buffer."""

    seq: int
    text: str


def add_progress(project_id: str, text: str) -> None:
    """Appends one progress event for *project_id*.

    Callers pass a complete, self-contained event line and no separator of
    their own; the Gateway's poller is what joins events with newlines when
    it renders them.

    Args:
        project_id: The project the event belongs to.
        text: The event to record -- a tool dispatch or its outcome, a
            task's result, a crash notice.
    """
    with _lock:
        _buffers.setdefault(project_id, []).append(text)


def get_chunks_since(project_id: str, since_seq: int = -1) -> list[ProgressChunk]:
    """Returns every chunk recorded for *project_id* after *since_seq*, in order.

    Args:
        project_id: The project to read buffered progress for.
        since_seq: The highest sequence number the caller has already seen;
            only chunks with a strictly greater seq are returned. -1 (the
            default) returns everything recorded so far.

    Returns:
        Chunks in recording order. A project_id with no chunks recorded yet
        (including one that doesn't exist) returns an empty list rather than
        raising.
    """
    with _lock:
        chunks = _buffers.get(project_id, [])
        return [
            ProgressChunk(seq=seq, text=text)
            for seq, text in enumerate(chunks)
            if seq > since_seq
        ]


def clear_progress(project_id: str) -> None:
    """Discards all buffered progress for *project_id*.

    Called once a project reaches a terminal phase (`done`/`blocked`) so the
    buffer doesn't grow unbounded across a project's full lifetime.

    Args:
        project_id: The project whose buffered progress should be discarded.
    """
    with _lock:
        _buffers.pop(project_id, None)
