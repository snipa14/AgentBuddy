"""Entrypoint: runs the Orchestrator's FastAPI app."""

import logging
from pathlib import Path

import httpx
import uvicorn

from services.orchestrator.api import build_app
from services.orchestrator.config import OrchestratorConfig
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.memory.global_store import build_global_memory_connection
from services.orchestrator.model_router.config import load_models_config
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.service import OrchestratorService

DEFAULT_MODELS_CONFIG_PATH = Path("config/models.yaml")
DEFAULT_SECRETS_DIR = Path("/run/secrets")


def _build_router() -> ModelRouter:
    models_config = load_models_config(DEFAULT_MODELS_CONFIG_PATH)
    anthropic_api_key = (DEFAULT_SECRETS_DIR / "claude_api_key").read_text().strip()
    return ModelRouter(models_config, anthropic_api_key=anthropic_api_key)


def main() -> None:
    """Reads config from the environment and serves the Orchestrator API."""
    logging.basicConfig(level=logging.INFO)
    config = OrchestratorConfig.from_env()
    checkpointer = build_sqlite_checkpointer(config.memory_db_path)
    memory_conn = build_global_memory_connection(config.memory_db_path)
    executor_client = ExecutorClient(config.executor_url, httpx.AsyncClient())
    service = OrchestratorService(
        checkpointer,
        _build_router(),
        executor_client,
        memory_conn,
        config.projects_root,
        config.global_cost_threshold_usd,
    )
    app = build_app(service)
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
