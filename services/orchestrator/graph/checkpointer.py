"""SQLite-backed LangGraph checkpointer (ADR-008)."""

import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver


def build_sqlite_checkpointer(db_path: Path) -> SqliteSaver:
    """Builds a SqliteSaver checkpointer backed by a file on disk.

    A dedicated `sqlite3.Connection` is opened per call rather than reused,
    so that a caller can prove restart-survival by closing this connection
    and opening a fresh one against the same file (simulating a process
    restart) -- see `services/orchestrator/graph/tests/unit/test_graph_end_to_end.py`.

    Args:
        db_path: Path to the sqlite database file. Parent directories are
            created if missing.

    Returns:
        A SqliteSaver wrapping a new sqlite3 connection to db_path.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)
