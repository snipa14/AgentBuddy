"""Orchestrator runtime configuration, sourced from environment variables."""

import os
from pathlib import Path

from pydantic import BaseModel

DEFAULT_MEMORY_DB_PATH = Path("/data/memory/foundry_memory.db")
DEFAULT_EXECUTOR_URL = "http://executor:8000"
DEFAULT_PROJECTS_ROOT = Path("/data/projects")
DEFAULT_GLOBAL_COST_THRESHOLD_USD = 50.0


class OrchestratorConfig(BaseModel):
    """Runtime configuration for the Orchestrator service."""

    memory_db_path: Path
    executor_url: str
    projects_root: Path
    global_cost_threshold_usd: float

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        """Builds a config from environment variables.

        Returns:
            A validated OrchestratorConfig, defaulting `memory_db_path` to
            the `foundry_memory` volume's mount point and `executor_url` to
            the Executor's service name (both from docker-compose.yml) when
            `MEMORY_DB_PATH`/`EXECUTOR_URL` are unset.  `projects_root`
            defaults to ``/data/projects``. `global_cost_threshold_usd`
            defaults to `50.0` when `GLOBAL_COST_THRESHOLD_USD` is unset.
        """
        raw_path = os.environ.get("MEMORY_DB_PATH")
        memory_db_path = Path(raw_path) if raw_path else DEFAULT_MEMORY_DB_PATH
        executor_url = os.environ.get("EXECUTOR_URL", DEFAULT_EXECUTOR_URL)
        raw_projects = os.environ.get("PROJECTS_ROOT")
        projects_root = Path(raw_projects) if raw_projects else DEFAULT_PROJECTS_ROOT
        raw_threshold = os.environ.get("GLOBAL_COST_THRESHOLD_USD")
        global_cost_threshold_usd = (
            float(raw_threshold) if raw_threshold else DEFAULT_GLOBAL_COST_THRESHOLD_USD
        )
        return cls(
            memory_db_path=memory_db_path,
            executor_url=executor_url,
            projects_root=projects_root,
            global_cost_threshold_usd=global_cost_threshold_usd,
        )
