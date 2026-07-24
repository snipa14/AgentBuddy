"""Executor runtime configuration, sourced from environment variables."""

import os
from pathlib import Path

from pydantic import BaseModel, Field

from services.executor.stacks import StacksConfig, load_stacks_config

DEFAULT_WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DEFAULT_NETWORK_NAME = "foundry_workspace"
DEFAULT_MEM_LIMIT = "2g"
DEFAULT_NANO_CPUS = 2_000_000_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_STACKS_CONFIG_PATH = "config/stacks.yaml"


class ExecutorConfig(BaseModel):
    """Runtime configuration for the Executor, validated once at process startup.

    mem_limit and nano_cpus are process-wide defaults for v1; Phase 7's
    projects.yaml is expected to add per-project overrides on top of these.
    `stacks` is the validated stack registry (ADR-045); it defaults to an
    empty registry so tests can construct a config without touching the
    filesystem, while from_env always loads the real config/stacks.yaml.
    """

    projects_host_path: Path
    workspace_image: str = DEFAULT_WORKSPACE_IMAGE
    network_name: str = DEFAULT_NETWORK_NAME
    mem_limit: str = DEFAULT_MEM_LIMIT
    nano_cpus: int = DEFAULT_NANO_CPUS
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS
    stacks: StacksConfig = Field(default_factory=lambda: StacksConfig(stacks={}))

    @classmethod
    def from_env(cls) -> "ExecutorConfig":
        """Builds a config from environment variables.

        Returns:
            An ExecutorConfig populated from the environment, falling back
            to module-level defaults for anything not set.

        Raises:
            ValueError: If PROJECTS_HOST_PATH is not set. The Executor's own
                container doesn't have projects/ bind-mounted (only the
                Orchestrator does); it needs the *host* absolute path so
                that bind mounts it asks the Docker daemon to create for
                workspace containers resolve correctly (Docker mounts are
                always resolved by the daemon against the host filesystem,
                never against the caller's own container filesystem).
            FileNotFoundError: If STACKS_CONFIG_PATH (or the
                DEFAULT_STACKS_CONFIG_PATH fallback, config/stacks.yaml)
                does not exist. The stack registry is required at startup,
                not lazily loaded per-request -- a missing file fails fast
                rather than degrading every container launch silently.
        """
        raw_path = os.environ.get("PROJECTS_HOST_PATH")
        if not raw_path:
            raise ValueError(
                "PROJECTS_HOST_PATH must be set to the absolute host path of "
                "the projects/ directory (see ARCHITECTURE.md §4.3)."
            )
        stacks = load_stacks_config(
            Path(os.environ.get("STACKS_CONFIG_PATH", DEFAULT_STACKS_CONFIG_PATH))
        )
        return cls(
            projects_host_path=Path(raw_path),
            workspace_image=os.environ.get("WORKSPACE_IMAGE", DEFAULT_WORKSPACE_IMAGE),
            network_name=os.environ.get("WORKSPACE_NETWORK", DEFAULT_NETWORK_NAME),
            mem_limit=os.environ.get("WORKSPACE_MEM_LIMIT", DEFAULT_MEM_LIMIT),
            nano_cpus=int(os.environ.get("WORKSPACE_NANO_CPUS", str(DEFAULT_NANO_CPUS))),
            command_timeout_seconds=int(
                os.environ.get(
                    "WORKSPACE_COMMAND_TIMEOUT_SECONDS", str(DEFAULT_COMMAND_TIMEOUT_SECONDS)
                )
            ),
            stacks=stacks,
        )


def image_for_stack(stack: str, config: "ExecutorConfig") -> str:
    """Resolves the workspace image for a project's stack (ADR-028, ADR-045).

    Args:
        stack: The project's declared stack (e.g. "python", "node", "go").
        config: The Executor's runtime configuration.

    Returns:
        The registered image for *stack* in config.stacks, otherwise
        config.workspace_image -- so a stack with no registry entry still
        gets a working (if not ideal) container instead of a hard failure.
    """
    entry = config.stacks.stacks.get(stack)
    return entry.image if entry is not None else config.workspace_image
