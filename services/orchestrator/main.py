"""Entrypoint: runs the Orchestrator's FastAPI app."""

import logging

import uvicorn

from services.orchestrator.api import build_app
from services.orchestrator.config import OrchestratorConfig
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.service import OrchestratorService


def main() -> None:
    """Reads config from the environment and serves the Orchestrator API."""
    logging.basicConfig(level=logging.INFO)
    config = OrchestratorConfig.from_env()
    checkpointer = build_sqlite_checkpointer(config.memory_db_path)
    service = OrchestratorService(checkpointer)
    app = build_app(service)
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
