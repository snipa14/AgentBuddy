"""Pydantic schema for a single global memory row (ARCHITECTURE.md §4.5)."""

from typing import Literal

from pydantic import BaseModel

MemoryType = Literal["error_pattern", "fix", "preference", "skill_usage"]


class Memory(BaseModel):
    """A single row from the global `memories` table."""

    id: int
    project_id: str | None
    type: MemoryType
    content: str
    tags: str
    created_at: str
