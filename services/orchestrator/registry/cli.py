"""`new-project` CLI entrypoint (ARCHITECTURE.md §4.6, TODO.md Phase 7).

Runs inside the Orchestrator container -- per .claude/rules/security.md the
host only has Docker installed, so `./foundry new-project` shells out to
this module via `docker compose exec orchestrator`. That only actually
works once a later phase gives the orchestrator service a real Dockerfile
(it's currently the `python:3.12-slim` / `sleep infinity` placeholder in
docker-compose.yml); until then this module is exercised directly by tests
and by running it locally, not through the CLI wrapper.

`_start_intake` imports LangGraph-dependent modules lazily, inside the
function body rather than at module load time, so this module (and its
argument-parsing logic) stays importable and testable even when langgraph
isn't installed -- the same fix Phase 6 applied to
`services/orchestrator/__init__.py`.
"""

import argparse
import sys
from pathlib import Path

from services.orchestrator.registry.errors import RegistryError
from services.orchestrator.registry.registration import (
    register_brownfield_from_git,
    register_brownfield_from_local_path,
    register_greenfield,
)

DEFAULT_PROJECTS_ROOT = Path("/data/projects")
DEFAULT_REGISTRY_PATH = DEFAULT_PROJECTS_ROOT / "projects.yaml"


def build_parser() -> argparse.ArgumentParser:
    """Builds the `new-project` argument parser."""
    parser = argparse.ArgumentParser(prog="new-project")
    parser.add_argument("project_id")
    parser.add_argument("--name", required=True)
    parser.add_argument("--stack", required=True)
    parser.add_argument("--coverage-threshold", type=int, default=80)
    parser.add_argument(
        "--pr-mode", choices=["auto", "manual", "configurable"], default="manual"
    )
    parser.add_argument(
        "--coverage-gate-mode", choices=["hard", "soft", "configurable"], default="hard"
    )
    parser.add_argument("--cost-ceiling-usd-weekly", type=float, default=None)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--from-path", type=Path, help="Link an existing local directory in as brownfield"
    )
    source.add_argument("--git-remote", help="Clone a git remote in as brownfield")
    return parser


def _start_intake(project_id: str, description: str) -> None:
    """Starts the Planner's intake interview for a newly registered project.

    Args:
        project_id: The project whose intake thread should be started.
        description: Seeds the Planner's intake interview (ARCHITECTURE.md
            §6.1) -- `new-project` has no free-text request the way a
            Telegram `/new` message would, so this is built from the
            registration's own --name/--stack arguments.
    """
    import httpx

    from services.orchestrator.config import OrchestratorConfig
    from services.orchestrator.executor_client import ExecutorClient
    from services.orchestrator.graph.checkpointer import build_sqlite_checkpointer
    from services.orchestrator.model_router.config import load_models_config
    from services.orchestrator.model_router.router import ModelRouter
    from services.orchestrator.service import OrchestratorService

    models_config = load_models_config(Path("config/models.yaml"))
    anthropic_api_key = Path("/run/secrets/claude_api_key").read_text().strip()
    router = ModelRouter(models_config, httpx.AsyncClient(), anthropic_api_key=anthropic_api_key)
    config = OrchestratorConfig.from_env()
    checkpointer = build_sqlite_checkpointer(config.memory_db_path)
    executor_client = ExecutorClient(config.executor_url, httpx.AsyncClient())
    OrchestratorService(checkpointer, router, executor_client).handle_webhook(
        project_id, description
    )


def main(argv: list[str] | None = None) -> int:
    """Parses arguments and registers a new project.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults
            to `sys.argv[1:]` when None.

    Returns:
        0 on success, 1 if registration failed.
    """
    args = build_parser().parse_args(argv)
    description = f"{args.name}: a {args.stack} project"

    def on_registered(project_id: str) -> None:
        _start_intake(project_id, description)

    try:
        if args.from_path is not None:
            register_brownfield_from_local_path(
                DEFAULT_REGISTRY_PATH,
                DEFAULT_PROJECTS_ROOT,
                args.project_id,
                args.name,
                args.from_path,
                args.stack,
                args.coverage_threshold,
                args.pr_mode,
                args.coverage_gate_mode,
                args.cost_ceiling_usd_weekly,
                on_registered=on_registered,
            )
        elif args.git_remote is not None:
            register_brownfield_from_git(
                DEFAULT_REGISTRY_PATH,
                DEFAULT_PROJECTS_ROOT,
                args.project_id,
                args.name,
                args.git_remote,
                args.stack,
                args.coverage_threshold,
                args.pr_mode,
                args.coverage_gate_mode,
                args.cost_ceiling_usd_weekly,
                on_registered=on_registered,
            )
        else:
            register_greenfield(
                DEFAULT_REGISTRY_PATH,
                DEFAULT_PROJECTS_ROOT,
                args.project_id,
                args.name,
                args.stack,
                args.coverage_threshold,
                args.pr_mode,
                args.coverage_gate_mode,
                args.cost_ceiling_usd_weekly,
                on_registered=on_registered,
            )
    except RegistryError as exc:
        print(f"new-project: {exc}", file=sys.stderr)
        return 1
    print(f"registered project {args.project_id!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
