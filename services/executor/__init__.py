"""Foundry executor: the only service allowed to talk to the Docker control surface.

Exposes five typed tool functions (tool_run_command, tool_read_file,
tool_write_file, tool_list_files, tool_run_tests) that create a fresh,
locked-down workspace container per call. See SECURITY.md for the threat
model and ARCHITECTURE.md §4.3 for the design.
"""

from services.executor.config import ExecutorConfig
from services.executor.docker_client import build_docker_client
from services.executor.errors import (
    CommandTimeoutError,
    ContainerExecutionError,
    ExecutorError,
    PathTraversalError,
)
from services.executor.schemas import CommandResult, FileEntry, TestResult
from services.executor.tools import (
    tool_list_files,
    tool_read_file,
    tool_run_command,
    tool_run_tests,
    tool_write_file,
)

__all__ = [
    "CommandResult",
    "CommandTimeoutError",
    "ContainerExecutionError",
    "ExecutorConfig",
    "ExecutorError",
    "FileEntry",
    "PathTraversalError",
    "TestResult",
    "build_docker_client",
    "tool_list_files",
    "tool_read_file",
    "tool_run_command",
    "tool_run_tests",
    "tool_write_file",
]
