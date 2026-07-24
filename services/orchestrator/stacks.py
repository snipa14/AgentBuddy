"""Pydantic schema and loader for config/stacks.yaml (ADR-045) -- Orchestrator copy.

A deliberate duplicate of services/executor/stacks.py rather than a
cross-service import: the two services communicate over HTTP and never
import each other's packages (the same decoupling executor_client.py's
duplicated result schemas already follow). Both copies validate the same
file at startup, so schema drift between them fails fast rather than
silently. The Orchestrator's consumers are the registry CLI's --stack
choices and Phase 23's static-checks node (check_commands).
"""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class StackManifest(BaseModel):
    """A dependency manifest file and the install command it triggers.

    Attributes:
        file: The manifest's filename at the project root (e.g.
            "requirements.txt", "package.json", "go.mod").
        install_command: The argv list run inside the workspace container
            when the manifest is present.
    """

    file: str = Field(min_length=1)
    install_command: list[str] = Field(min_length=1)


class StackCheck(BaseModel):
    """A deterministic static-check command for Phase 23's checks node.

    Attributes:
        command: The argv list to run inside the workspace container.
        requires_file: A project-root file that must exist for this check to
            apply (e.g. "tsconfig.json" for tsc). None means the check
            always applies. A missing required file skips the check, never
            fails it.
    """

    command: list[str] = Field(min_length=1)
    requires_file: str | None = None


class StackConfig(BaseModel):
    """One stack's full declaration in stacks.yaml.

    Attributes:
        image: The workspace image tag the Executor launches for this stack.
        base_image: The upstream base image (pinned tag) the generator
            renders this stack's Dockerfile FROM.
        apt_packages: Extra apt packages baked into the image.
        pip_packages: pip packages baked into the image. Every workspace
            image needs at least Playwright here -- the Executor's own tools
            run Python scripts in every container regardless of the
            project's stack (ADR-028).
        pip_break_system_packages: Whether the image's pip install needs
            --break-system-packages (Debian-managed Python, e.g. the node
            and go base images).
        sandbox_user_mode: "create" runs groupadd/useradd for the sandbox
            user; "rename" renames an existing UID/GID-1000 user instead
            (node:20-slim ships a "node" user on that UID).
        rename_from: The existing username to rename when sandbox_user_mode
            is "rename".
        env: Extra ENV lines rendered into the Dockerfile (e.g. GOMODCACHE).
        manifests: Dependency manifests checked in order; the first one
            present in the project wins.
        cache_dir: The in-container package-manager cache directory the
            per-project named cache volume mounts over (ADR-028). None
            means the stack gets no cache volume.
        check_commands: Deterministic static checks for Phase 23.
        test_command: The argv list tool_run_tests runs for this stack.
            None falls back to the caller-supplied framework parameter's
            legacy pytest/npm behavior.
    """

    image: str = Field(min_length=1)
    base_image: str = Field(min_length=1)
    apt_packages: list[str] = Field(default_factory=list)
    pip_packages: list[str] = Field(default_factory=list)
    pip_break_system_packages: bool = False
    sandbox_user_mode: Literal["create", "rename"] = "create"
    rename_from: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    manifests: list[StackManifest] = Field(default_factory=list)
    cache_dir: str | None = None
    check_commands: list[StackCheck] = Field(default_factory=list)
    test_command: list[str] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> "StackConfig":
        if self.sandbox_user_mode == "rename" and not self.rename_from:
            raise ValueError("sandbox_user_mode 'rename' requires rename_from")
        if self.cache_dir is not None and not self.cache_dir.startswith("/"):
            raise ValueError(f"cache_dir must be absolute, got {self.cache_dir!r}")
        return self


class StacksConfig(BaseModel):
    """The full, validated contents of a stacks.yaml file."""

    stacks: dict[str, StackConfig]


def load_stacks_config(path: Path) -> StacksConfig:
    """Loads and validates a stacks.yaml file.

    Imports yaml lazily, inside the function body rather than at module load
    time, so this module's Pydantic schema stays importable and testable
    even when pyyaml isn't installed -- the same fix
    services/orchestrator/registry/cli.py applies for langgraph.

    Args:
        path: Path to a YAML file matching the StacksConfig schema.

    Returns:
        A validated StacksConfig instance.

    Raises:
        FileNotFoundError: If path does not exist.
        pydantic.ValidationError: If the file's contents don't match the schema.
    """
    import yaml

    raw = yaml.safe_load(path.read_text())
    return StacksConfig.model_validate(raw)
