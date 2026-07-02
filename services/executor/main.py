"""Entrypoint: runs the Executor's FastAPI app."""

import logging

import uvicorn

from services.executor.api import build_app
from services.executor.config import ExecutorConfig
from services.executor.docker_client import build_docker_client


def main() -> None:
    """Reads config from the environment and serves the Executor API."""
    logging.basicConfig(level=logging.INFO)
    config = ExecutorConfig.from_env()
    client = build_docker_client()
    app = build_app(client, config)
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
