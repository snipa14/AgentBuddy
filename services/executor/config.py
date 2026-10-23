"""Executor runtime configuration, sourced from environment variables.

DEFAULT_WORKSPACE_IMAGE and DEFAULT_NETWORK_NAME below are only the
from_env() fallback and the default used by tests that construct an
ExecutorConfig directly without going through env vars -- the values
actually deployed come from docker-compose.yml's executor.environment
block (WORKSPACE_IMAGE/WORKSPACE_NETWORK), which is the source of truth.
"""

import os
from pathlib import Path

from pydantic import BaseModel, Field

from services.executor.stacks import StacksConfig, load_stacks_config

DEFAULT_WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DEFAULT_NETWORK_NAME = "foundry_workspace"
DEFAULT_MEM_LIMIT = "2g"
DEFAULT_NANO_CPUS = 2_000_000_000
DEFAULT_PIDS_LIMIT = 256
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300
DEFAULT_STACKS_CONFIG_PATH = "config/stacks.yaml"


class ExecutorConfig(BaseModel):
    """Runtime configuration for the Executor, validated once at process startup.

    mem_limit, nano_cpus, and pids_limit are process-wide defaults for v1; Phase 7's
    projects.yaml is expected to add per-project overrides on top of these.
    mem_limit is passed to Docker as both the memory and the memory+swap
    ceiling (ADR-059), so it is the real total a workspace container can
    reach rather than half of it.
    `stacks` is the validated stack registry (ADR-045); it defaults to an
    empty registry so tests can construct a config without touching the
    filesystem, while from_env always loads the real config/stacks.yaml.
    """

    projects_host_path: Path
    workspace_image: str = DEFAULT_WORKSPACE_IMAGE
    network_name: str = DEFAULT_NETWORK_NAME
    mem_limit: str = DEFAULT_MEM_LIMIT
    nano_cpus: int = DEFAULT_NANO_CPUS
    pids_limit: int = DEFAULT_PIDS_LIMIT
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
            Path(_env_or_default("STACKS_CONFIG_PATH", DEFAULT_STACKS_CONFIG_PATH))
        )
        return cls(
            projects_host_path=Path(raw_path),
            workspace_image=_env_or_default("WORKSPACE_IMAGE", DEFAULT_WORKSPACE_IMAGE),
            network_name=_env_or_default("WORKSPACE_NETWORK", DEFAULT_NETWORK_NAME),
            mem_limit=_env_or_default("WORKSPACE_MEM_LIMIT", DEFAULT_MEM_LIMIT),
            nano_cpus=int(_env_or_default("WORKSPACE_NANO_CPUS", str(DEFAULT_NANO_CPUS))),
            pids_limit=int(_env_or_default("WORKSPACE_PIDS_LIMIT", str(DEFAULT_PIDS_LIMIT))),
            command_timeout_seconds=int(
                _env_or_default(
                    "WORKSPACE_COMMAND_TIMEOUT_SECONDS", str(DEFAULT_COMMAND_TIMEOUT_SECONDS)
                )
            ),
            stacks=stacks,
        )


def _env_or_default(key: str, default: str) -> str:
    """Reads an environment variable, treating both "unset" and "" as absent.

    os.environ.get(key, default) only falls back to default when key is
    entirely absent from the environment -- a variable explicitly set to the
    empty string (common with docker-compose env-var interpolation of an
    unset host variable) passes straight through as "", which then breaks
    int()-parsed settings like WORKSPACE_NANO_CPUS with a raw ValueError
    instead of the documented "falls back to the default" behavior.

    Args:
        key: The environment variable's name.
        default: The value to use when key is unset or set to "".

    Returns:
        The environment variable's value, or default.
    """
    value = os.environ.get(key)
    return value if value else default


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
