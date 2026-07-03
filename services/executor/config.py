"""Executor runtime configuration, sourced from environment variables."""

import os
from pathlib import Path

from pydantic import BaseModel

DEFAULT_WORKSPACE_IMAGE = "foundry/workspace-python:3.12"
DEFAULT_NETWORK_NAME = "foundry_workspace"
DEFAULT_MEM_LIMIT = "2g"
DEFAULT_NANO_CPUS = 2_000_000_000
DEFAULT_COMMAND_TIMEOUT_SECONDS = 300

STACK_IMAGES: dict[str, str] = {
    "python": DEFAULT_WORKSPACE_IMAGE,
    "node": "foundry/workspace-node:20",
}


class ExecutorConfig(BaseModel):
    """Runtime configuration for the Executor, validated once at process startup.

    mem_limit and nano_cpus are process-wide defaults for v1; Phase 7's
    projects.yaml is expected to add per-project overrides on top of these.
    """

    projects_host_path: Path
    workspace_image: str = DEFAULT_WORKSPACE_IMAGE
    network_name: str = DEFAULT_NETWORK_NAME
    mem_limit: str = DEFAULT_MEM_LIMIT
    nano_cpus: int = DEFAULT_NANO_CPUS
    command_timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS

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
        """
        raw_path = os.environ.get("PROJECTS_HOST_PATH")
        if not raw_path:
            raise ValueError(
                "PROJECTS_HOST_PATH must be set to the absolute host path of "
                "the projects/ directory (see ARCHITECTURE.md §4.3)."
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
        )


def image_for_stack(stack: str, config: "ExecutorConfig") -> str:
    """Resolves the workspace image for a project's stack (ADR-028, TODO.md Phase 17).

    Args:
        stack: The project's declared stack (e.g. "python", "node").
        config: The Executor's runtime configuration.

    Returns:
        The dedicated image for *stack* if one is registered in
        STACK_IMAGES, otherwise config.workspace_image -- so a stack with no
        dedicated image yet still gets a working (if not ideal) container
        instead of a hard failure.
    """
    return STACK_IMAGES.get(stack, config.workspace_image)
