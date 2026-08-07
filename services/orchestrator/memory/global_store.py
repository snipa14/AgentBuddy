"""Global cross-project memory store: SQLite + FTS5 (ARCHITECTURE.md §4.5).

`memories_fts` is an external-content FTS5 table (content='memories'), kept
in sync via triggers rather than duplicating row content into the index
table -- the standard SQLite pattern for full-text search over a table that
also needs normal column access (project_id, type, created_at) that FTS5
itself doesn't support querying on.
"""

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from services.orchestrator.memory.schemas import Memory, MemoryType

logger = logging.getLogger(__name__)

_RECALL_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{3,}")
_MAX_RECALL_TOKENS = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    project_id TEXT,
    type TEXT,
    content TEXT,
    tags TEXT,
    created_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, tags, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
        VALUES ('delete', old.id, old.content, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, tags)
        VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO memories_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
"""


class GlobalMemoryConnection:
    """Pairs the shared global-memory sqlite3.Connection with the lock that
    serializes every thread's access to it.

    The Orchestrator holds exactly one of these for its whole process
    lifetime (main.py), shared across every project's graph invocation, so
    `check_same_thread=False` alone is not enough: it only disables
    sqlite3's same-thread check, it does not make a single sqlite3.Connection
    safe for concurrent statement execution. FastAPI can run concurrent sync
    route handlers in its threadpool -- e.g. one project's Telegram approval
    callback resuming its graph while another project's webhook is
    mid-flight -- so two threads calling record_memory/recall at close to
    the same time can otherwise corrupt cursor/transaction state or raise
    sqlite3.OperationalError. Every function in this module that touches
    `.conn` acquires `.lock` first, mirroring how model_router.router's
    _ThrottledChatModel wraps a shared model with the semaphore that
    serializes access to it -- one lock per shared connection, held only for
    the execute()/commit() (or execute()/fetchall()) sequence that needs it.

    Delegates any attribute not defined here (close(), etc.) straight to the
    wrapped connection, so callers that treat the return value of
    build_global_memory_connection() as an opaque handle -- the common case
    -- don't need to change how they use it.
    """

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self.conn = conn
        self.lock = lock

    def __getattr__(self, name: str) -> Any:
        """Delegates any attribute not defined on this wrapper to the wrapped connection."""
        return getattr(self.conn, name)


def build_global_memory_connection(db_path: Path) -> GlobalMemoryConnection:
    """Opens the global memory database, creating and migrating it if needed.

    Args:
        db_path: Path to the sqlite database file. Parent directories are
            created if missing.

    Returns:
        A GlobalMemoryConnection wrapping an open sqlite3.Connection (with
        the memories/memories_fts schema applied) and a threading.Lock
        that's 1:1 with this connection -- every read/write in this module
        acquires it before touching the connection, since the connection is
        shared across every project's graph invocation for the Orchestrator
        process's lifetime and is not safe for unsynchronized concurrent use.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.executescript(_SCHEMA)
    conn.commit()
    return GlobalMemoryConnection(conn, threading.Lock())


def record_memory(
    memory_conn: GlobalMemoryConnection,
    project_id: str | None,
    type_: MemoryType,
    content: str,
    tags: str,
    created_at: str,
) -> int:
    """Inserts a single global memory row.

    Args:
        memory_conn: A GlobalMemoryConnection from build_global_memory_connection().
            The insert and its commit happen under `memory_conn.lock`, so
            concurrent callers on the shared connection serialize instead of
            racing.
        project_id: The project this memory originated from, or None for a
            memory that isn't tied to any single project.
        type_: One of "error_pattern", "fix", "preference", "skill_usage".
        content: The memory's free-text content, matched by recall().
        tags: Space-separated tags, also matched by recall().
        created_at: An ISO-8601 timestamp. Caller-supplied rather than
            computed here, so callers stay in control of clock access and
            tests can assert on an exact, known value.

    Returns:
        The new row's id.
    """
    with memory_conn.lock:
        cursor = memory_conn.conn.execute(
            "INSERT INTO memories (project_id, type, content, tags, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, type_, content, tags, created_at),
        )
        memory_conn.conn.commit()
        return cursor.lastrowid


def recall_for_task(
    memory_conn: GlobalMemoryConnection, task_description: str, limit: int = 5
) -> list[str]:
    """Recalls cross-project lessons relevant to a free-text task description.

    `recall()` takes a raw FTS5 MATCH expression, where quotes, parens, and
    bare uppercase AND/OR/NOT are syntax -- passing arbitrary task text
    through would raise sqlite3.OperationalError on the first hostile
    string. This entrypoint sanitizes instead: it extracts plain
    alphanumeric word tokens (lowercased, so none can collide with FTS5's
    case-sensitive uppercase operators), deduplicates them, and ORs the
    first few together.

    Args:
        memory_conn: A GlobalMemoryConnection from build_global_memory_connection().
        task_description: The task's free-text description (a task_queue
            item's `description`).
        limit: Maximum number of lesson strings to return, best-ranked first.

    Returns:
        Up to `limit` matching memories' content strings, ordered by FTS5
        rank. Empty if the description yields no usable tokens, nothing
        matches, or the query fails (logged, never raised -- recall is an
        enrichment, not a dependency).
    """
    tokens = list(
        dict.fromkeys(
            token.lower() for token in _RECALL_TOKEN_RE.findall(task_description)
        )
    )[:_MAX_RECALL_TOKENS]
    if not tokens:
        return []
    query = " OR ".join(tokens)
    try:
        memories = recall(memory_conn, query)
    except sqlite3.OperationalError:
        logger.warning("recall_for_task query failed for %r", query, exc_info=True)
        return []
    return [memory.content for memory in memories[:limit]]


def recall(
    memory_conn: GlobalMemoryConnection, query: str, project_id: str | None = None
) -> list[Memory]:
    """Full-text searches memory content and tags via FTS5 MATCH.

    Args:
        memory_conn: A GlobalMemoryConnection from build_global_memory_connection().
            The query runs under `memory_conn.lock`, so concurrent callers
            on the shared connection serialize instead of racing.
        query: An FTS5 match expression (e.g. a plain word or phrase).
        project_id: If given, restricts results to that project's memories.
            If None, searches across every project.

    Returns:
        Matching memories ordered by FTS5 rank (best match first).
    """
    sql = (
        "SELECT memories.id, memories.project_id, memories.type, memories.content, "
        "memories.tags, memories.created_at "
        "FROM memories_fts "
        "JOIN memories ON memories.id = memories_fts.rowid "
        "WHERE memories_fts MATCH ?"
    )
    params: list[str] = [query]
    if project_id is not None:
        sql += " AND memories.project_id = ?"
        params.append(project_id)
    sql += " ORDER BY rank"

    with memory_conn.lock:
        rows = memory_conn.conn.execute(sql, params).fetchall()
    return [
        Memory(
            id=row[0],
            project_id=row[1],
            type=row[2],
            content=row[3],
            tags=row[4],
            created_at=row[5],
        )
        for row in rows
    ]
