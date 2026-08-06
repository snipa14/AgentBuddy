"""Entrypoint: runs the Orchestrator's FastAPI app."""

import logging
import os
from pathlib import Path

import uvicorn

from services.orchestrator.api import build_app
from services.orchestrator.config import OrchestratorConfig
from services.orchestrator.executor_client import ExecutorClient
from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
from services.orchestrator.memory.global_store import build_global_memory_connection
from services.orchestrator.model_router.config import load_models_config
from services.orchestrator.model_router.errors import NoCapableProviderError
from services.orchestrator.model_router.router import ModelRouter
from services.orchestrator.plugins.discovery import (
    LoadedPlugin,
    discover_plugins,
    synthesize_plugin_roles,
)
from services.orchestrator.service import OrchestratorService
from services.orchestrator.stacks import load_stacks_config

DEFAULT_MODELS_CONFIG_PATH = "config/models.yaml"
DEFAULT_STACKS_CONFIG_PATH = "config/stacks.yaml"
DEFAULT_PLUGINS_DIR = "plugins"
DEFAULT_SECRETS_DIR = Path("/run/secrets")


def _build_router_and_plugins() -> tuple[ModelRouter, list[LoadedPlugin]]:
    """Loads model routing config, discovers plugins, and builds the router.

    Discovered plugins whose declared role can't resolve to a configured
    provider (missing required capability, e.g. no vision-capable model
    configured for a plugin that needs one) are dropped with a logged
    warning rather than failing startup -- the same skip-don't-crash
    contract discover_plugins itself already applies to a malformed
    manifest or missing skill file.

    Returns:
        A tuple of (router, resolvable_plugins) -- the ModelRouter built
        from models.yaml plus any synthesized plugin roles, and the subset
        of discovered plugins whose role actually resolves through it.
    """
    models_config_path = Path(os.environ.get("MODELS_CONFIG_PATH", DEFAULT_MODELS_CONFIG_PATH))
    models_config = load_models_config(models_config_path)
    plugins_dir = Path(os.environ.get("PLUGINS_DIR", DEFAULT_PLUGINS_DIR))
    discovered = discover_plugins(plugins_dir, known_roles=frozenset(models_config.roles))
    models_config = synthesize_plugin_roles(models_config, discovered)
    anthropic_api_key = (DEFAULT_SECRETS_DIR / "claude_api_key").read_text().strip()
    router = ModelRouter(models_config, anthropic_api_key=anthropic_api_key)

    resolvable_plugins = []
    for plugin in discovered:
        try:
            router.resolve(plugin.manifest.role)
        except NoCapableProviderError as exc:
            logging.getLogger(__name__).warning(
                "plugin '%s': no configured provider satisfies its required "
                "capabilities, skipping: %s", plugin.manifest.name, exc,
            )
            continue
        resolvable_plugins.append(plugin)
    return router, resolvable_plugins


def main() -> None:
    """Reads config from the environment and serves the Orchestrator API."""
    logging.basicConfig(level=logging.INFO)
    config = OrchestratorConfig.from_env()
    checkpointer = build_sqlite_checkpointer(config.memory_db_path)
    memory_conn = build_global_memory_connection(config.memory_db_path)
    executor_client = ExecutorClient(config.executor_url)
    stacks_config = load_stacks_config(
        Path(os.environ.get("STACKS_CONFIG_PATH", DEFAULT_STACKS_CONFIG_PATH))
    )
    router, plugins = _build_router_and_plugins()
    service = OrchestratorService(
        checkpointer,
        router,
        executor_client,
        memory_conn,
        config.projects_root,
        stacks_config,
        plugins,
        config.global_cost_threshold_usd,
    )
    app = build_app(service)
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
