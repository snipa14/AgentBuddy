"""Orchestrator runtime configuration, sourced from environment variables."""

import os
from pathlib import Path

from pydantic import BaseModel

DEFAULT_MEMORY_DB_PATH = Path("/data/memory/foundry_memory.db")


class OrchestratorConfig(BaseModel):
    """Runtime configuration for the Orchestrator service."""

    memory_db_path: Path

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        """Builds a config from environment variables.

        Returns:
            A validated OrchestratorConfig, defaulting `memory_db_path` to
            the `foundry_memory` volume's mount point (docker-compose.yml)
            when `MEMORY_DB_PATH` is unset.
        """
        raw_path = os.environ.get("MEMORY_DB_PATH")
        memory_db_path = Path(raw_path) if raw_path else DEFAULT_MEMORY_DB_PATH
        return cls(memory_db_path=memory_db_path)
