"""Orchestrator runtime configuration, sourced from environment variables."""

import os
from pathlib import Path

from pydantic import BaseModel

DEFAULT_MEMORY_DB_PATH = Path("/data/memory/foundry_memory.db")
DEFAULT_EXECUTOR_URL = "http://executor:8000"


class OrchestratorConfig(BaseModel):
    """Runtime configuration for the Orchestrator service."""

    memory_db_path: Path
    executor_url: str

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        """Builds a config from environment variables.

        Returns:
            A validated OrchestratorConfig, defaulting `memory_db_path` to
            the `foundry_memory` volume's mount point and `executor_url` to
            the Executor's service name (both from docker-compose.yml) when
            `MEMORY_DB_PATH`/`EXECUTOR_URL` are unset.
        """
        raw_path = os.environ.get("MEMORY_DB_PATH")
        memory_db_path = Path(raw_path) if raw_path else DEFAULT_MEMORY_DB_PATH
        executor_url = os.environ.get("EXECUTOR_URL", DEFAULT_EXECUTOR_URL)
        return cls(memory_db_path=memory_db_path, executor_url=executor_url)
