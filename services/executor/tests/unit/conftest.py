"""Shared fixtures for the Executor's unit tests."""

import pytest

from services.executor.stacks import StackConfig, StackManifest, StacksConfig


@pytest.fixture()
def two_stack_registry() -> StacksConfig:
    """A python+node StacksConfig mirroring config/stacks.yaml's entries."""
    return StacksConfig(
        stacks={
            "python": StackConfig(
                image="foundry/workspace-python:3.12",
                base_image="python:3.12-slim",
                manifests=[
                    StackManifest(
                        file="requirements.txt",
                        install_command=[
                            "pip", "install", "--user", "-r", "requirements.txt",
                        ],
                    ),
                    StackManifest(
                        file="pyproject.toml",
                        install_command=["pip", "install", "--user", "-e", "."],
                    ),
                ],
                cache_dir="/home/sandbox/.cache/pip",
                test_command=[
                    "python3", "-m", "pytest",
                    "--cov=.", "--cov-report=term-missing", "-q",
                ],
            ),
            "node": StackConfig(
                image="foundry/workspace-node:20",
                base_image="node:20-slim",
                sandbox_user_mode="rename",
                rename_from="node",
                manifests=[
                    StackManifest(
                        file="package.json", install_command=["npm", "install"]
                    )
                ],
                cache_dir="/home/sandbox/.npm",
                test_command=["npm", "test"],
            ),
        }
    )
